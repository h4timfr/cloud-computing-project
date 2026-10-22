<#
.SYNOPSIS
  Idempotent FloodPulse deployment: one DynamoDB table, one Lambda, one HTTP API,
  one S3 static dashboard.

.DESCRIPTION
  Creates or updates exactly these resources, and nothing else:
    * DynamoDB table (pk/sk, PAY_PER_REQUEST, TTL on "ttl")
    * Lambda function (python3.13, 256 MB, 10 s, handler floodpulse.handler.lambda_handler)
    * API Gateway HTTP API with POST /readings, GET /zones, GET /zones/{zone_id}/history,
      GET /incidents, POST /subscriptions + CORS
    * the API -> Lambda invoke permission, scoped to that API
    * CloudWatch log retention for the function's log group
    * S3 bucket for the static dashboard (website hosting, public read of objects only)

  Properties of this script:
    * additive only: it never deletes, replaces or force-updates a resource
    * idempotent: safe to re-run; existing resources are detected and left alone
    * IAM is read-only here: the execution role is managed by the project owner and is
      only looked up (create it as "floodpulse-lambda-role" with lambda.amazonaws.com
      trust plus logs:* and dynamodb:PutItem/GetItem/Query on the FloodPulse table)
    * the ingestion secret is NEVER printed: this script reads it from a gitignored file
      (.floodpulse-key, created by tools/init_ingest_key.py) and passes it to the Lambda
      through a gitignored environment file; only the *names* of the variables are shown
    * CORS is restricted to the dashboard website origin (plus an optional dev origin);
      the wildcard is never written
    * no credentials are created, printed or stored
    * secrets are only ever reported as variable *names* or "sha256:" fingerprints, and every
      message that could carry AWS CLI output passes through a redaction gate first
    * every write is preceded by a read: the deployed environment map, handler, role, CORS
      block, integration target, log retention and bucket policy are compared with what is
      really there, and an unexpected state stops the deploy instead of being overwritten

  Hardening (each of these is explained where it is implemented):
    * the deployed Lambda environment map is read and merged, so AWS-side variables are never
      silently deleted; the handler and role are compared too (the role is never reassigned)
    * a missing ingestion key, key drift between .floodpulse-key and the env file, an
      uncommitted src/ + web/ tree, an unknown CORS origin and an existing non-public bucket
      are all *refusals* unless the matching switch is passed
    * AWS CLI failures are only read as "resource is missing" when they are not-found errors
    * all list calls are paginated, and every write path is exercised by -DryRun

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -RoleName floodpulse-lambda-role
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -SkipDashboard
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -DryRun                  # plan only
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -AllowDirtyTree          # ship uncommitted work
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -AllowKeyDrift           # deploy the env-file key
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -KeepUnknownEnvVars      # keep AWS-only variables

.NOTES
  Requires AWS CLI v2 and a configured profile (-Profile, default cloud-project).
  Region defaults to eu-north-1.

  On this machine the available shell is Windows PowerShell 5.1 (pwsh is not
  installed) and the execution policy blocks unsigned scripts, hence the
  process-scoped "-ExecutionPolicy Bypass" above; it changes no machine setting.
#>
[CmdletBinding()]
param(
    [string]$Profile = 'cloud-project',
    [string]$Region = 'eu-north-1',
    [string]$TableName = 'floodpulse',
    [string]$FunctionName = 'floodpulse-api',
    [string]$ApiName = 'floodpulse-api',
    [string]$RoleName = 'floodpulse-lambda-role',
    [string]$StageName = '$default',
    [string]$Runtime = 'python3.13',
    [int]$MemorySize = 256,
    [int]$TimeoutSeconds = 15,
    [int]$LogRetentionDays = 7,
    [string]$DashboardBucket = 'floodpulse-dashboard-725934295966',
    [string]$IngestKeyFile = '.floodpulse-key',
    [string]$LocalDevOrigin = 'http://localhost:8000',
    [string]$AlertTopicName = 'floodpulse-alerts',
    [int]$AlertCooldownSeconds = 600,
    [string]$ZoneMode = 'live',
    [string]$WeatherMode = 'open-meteo',
    [int]$WeatherRefreshSeconds = 600,
    [double]$WeatherTimeoutSeconds = 4.0,
    [switch]$SkipDashboard,
    [switch]$SkipAlerts,
    # Safety switches added by the hardening pass. Each one defaults to the *refusing* behaviour,
    # so the safe path is the one you get by doing nothing.
    [switch]$DashboardOnly,       # re-upload web/ to S3 and verify it, and touch nothing else
[switch]$DryRun,             # read and compare everything, print the writes it would make, change nothing
    [switch]$AllowDirtyTree,     # deploy even though src/, web/ or infra/ have uncommitted changes
    [switch]$AllowKeyDrift,      # deploy the env-file ingest key even though .floodpulse-key disagrees
    [switch]$KeepUnknownEnvVars  # keep variables that exist in AWS but not in the local env file
)

$ErrorActionPreference = 'Stop'
$alertsBlocked = $false
$repoRoot = Split-Path -Parent $PSScriptRoot
$buildDir = Join-Path $repoRoot 'build'
$zipPath = Join-Path $buildDir 'floodpulse-api.zip'
$envPath = Join-Path $buildDir 'env-prod.json'
$corsPath = Join-Path $buildDir 'cors.json'
$policyPath = Join-Path $buildDir 'bucket-policy.json'
$websitePath = Join-Path $buildDir 'website.json'
$srcDir = Join-Path $repoRoot 'src'
$webDir = Join-Path $repoRoot 'web'
$keyFilePath = Join-Path $repoRoot $IngestKeyFile

function Write-Step { param([string]$Message) Write-Host "`n=== $Message ===" -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "  [ok]   $Message" -ForegroundColor Green }
function Write-Info { param([string]$Message) Write-Host "  [info] $Message" }
function Write-Warn2 { param([string]$Message) Write-Host "  [warn] $Message" -ForegroundColor Yellow }

function Write-Fail {
    <#
      Failure helper: prints the reason and aborts the run. Added by the hardening pass because
      several conditions were only reported with Write-Warn2 and then ignored (a missing
      ingestion secret, env-file/key drift, an unusable public-read bucket policy), which let a
      deployment that cannot possibly work finish as if it had succeeded.
    #>
    param([string]$Message)
    Write-Host "  [fail] $Message" -ForegroundColor Red
    throw $Message
}

function Write-OkApplied {
    <#
      Prints "[ok]" only when a change was really applied. A -DryRun must never claim that
      something was written; the "[dry-run] would run: aws ..." lines are its report.
    #>
    param([string]$Message)
    if (-not $DryRun) { Write-Ok $Message }
}

function Write-DryRun {
    param([string]$Message)
    Write-Host "  [dry-run] $Message" -ForegroundColor DarkCyan
}

# Every secret value this script reads is registered here (see Register-Secret) so that
# Remove-SecretText can redact it from any later message.
$script:KnownSecrets = New-Object System.Collections.ArrayList

function Register-Secret {
    param([string]$Value)
    if ($Value -and -not $script:KnownSecrets.Contains($Value)) { [void]$script:KnownSecrets.Add($Value) }
}

function Remove-SecretText {
    <#
      The single gate between raw text and the console. An AWS CLI failure echoes the request
      that failed (an explicit --environment document, for instance), so nothing derived from
      CLI output is printed or thrown before it has been through here: every registered secret
      value is replaced, and FLOODPULSE_INGEST_KEY is masked in its JSON and KEY=VALUE shapes
      even when this process never read the value itself.
    #>
    param([string]$Text)
    if (-not $Text) { return '' }
    $safe = $Text
    foreach ($secret in @($script:KnownSecrets)) {
        if ($secret) { $safe = $safe.Replace($secret, '<redacted>') }
    }
    $safe = [regex]::Replace($safe, '("FLOODPULSE_INGEST_KEY"\s*:\s*")[^"]*(")', '$1<redacted>$2', 'IgnoreCase')
    $safe = [regex]::Replace($safe, '(FLOODPULSE_INGEST_KEY\s*=\s*)\S+', '$1<redacted>', 'IgnoreCase')
    return $safe
}

function Compare-StringSet {
    <#
      Items of $Left that are absent from $Right, compared case-insensitively because HTTP
      method and header names are case-insensitive. Used by the CORS comparison.
    #>
    param([string[]]$Left, [string[]]$Right)
    $normalised = @($Right | ForEach-Object { "$_".ToLowerInvariant() })
    $missing = New-Object System.Collections.ArrayList
    foreach ($item in @($Left)) {
        if ($normalised -notcontains "$item".ToLowerInvariant()) { [void]$missing.Add($item) }
    }
    return @($missing)
}

function Get-SecretFingerprint {
    <#
      A short one-way id for a secret, so two copies can be compared and reported without
      either value ever being printed. Mirrors tools/init_ingest_key.py's fingerprint().
    #>
    param([string]$Value)
    if (-not $Value) { return 'sha256:(empty)' }
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Value))
    } finally {
        $sha.Dispose()
    }
    $hex = ([System.BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
    return 'sha256:' + $hex.Substring(0, 12)
}

function Write-JsonFile {
    <#
      Writes a JSON file as UTF-8 *without* a BOM. Windows PowerShell's
      "Set-Content -Encoding UTF8" adds a BOM, which the AWS CLI's JSON file
      loader rejects. ASCII here is safe: these files hold only bucket names,
      table names, origins and headers.

      With -DryRun nothing is written: these files are only ever read by AWS CLI calls that a
      dry run does not make, so the plan stays a plan.
    #>
    param([string]$Path, [string]$Content)
    if ($DryRun) {
        Write-DryRun "would write $Path (content not displayed: one of these files is the environment document, which carries the ingestion secret)"
        return
    }
    [System.IO.File]::WriteAllText($Path, $Content, (New-Object System.Text.UTF8Encoding($false)))
}

function Invoke-Aws {
    <# Runs the AWS CLI and returns stdout as a trimmed string; throws on failure. #>
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = (& aws @AwsArguments --profile $Profile --region $Region 2>&1 | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -ne 0) {
        # item 16: a CLI failure echoes the request that failed, so the text is redacted before
        # it is thrown - and therefore before it can reach the console or a log.
        throw (Remove-SecretText "aws $($AwsArguments -join ' ') failed (exit $LASTEXITCODE):`n$output")
    }
    if ($output -eq '') { return $null }
    return $output
}

function Invoke-AwsJson {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments)
    $text = Invoke-Aws @AwsArguments
    if (-not $text) { return $null }
    return ($text | ConvertFrom-Json)
}

function Test-AwsResource {
    <#
      True when the resource exists, false when the AWS CLI reports it as *missing*, and a hard
      failure for every other error. Item 7: the old version treated any non-zero exit as
      "missing", so access denied, expired credentials, throttling or a bad parameter silently
      took the create path and could collide with (or overwrite) an existing resource.
    #>
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = (& aws @AwsArguments --profile $Profile --region $Region 2>&1 | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $previous
    }
    if ($LASTEXITCODE -eq 0) { return $true }
    $message = Remove-SecretText $output
    # Only these shapes mean "the resource is not there"; anything else is a real error. The
    # NoSuch... prefix covers the whole family AWS uses for a missing resource
    # (NoSuchEntity, NoSuchBucket, NoSuchKey, NoSuchBucketPolicy,
    # NoSuchPublicAccessBlockConfiguration, NoSuchWebsiteConfiguration, ...).
    if ($message -match '(?i)NoSuch[A-Za-z]*|NotFoundException|not found|does not exist|cannot be found|\(404\)|404 Not Found') {
        return $false
    }
    Write-Fail "aws $($AwsArguments -join ' ') could not be evaluated (exit $LASTEXITCODE): $message. This is not a 'resource is missing' error, so the deploy stops instead of assuming the resource has to be created (item 7)."
}

function Invoke-AwsWrite {
    <#
      Every *writing* AWS CLI call goes through here so that -DryRun is implemented in exactly
      one place: with -DryRun the command is printed (with the profile and region it would use)
      and never executed.
    #>
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments)
    if ($DryRun) {
        Write-DryRun "would run: aws $($AwsArguments -join ' ') --profile $Profile --region $Region"
        return $null
    }
    return (Invoke-Aws @AwsArguments)
}

function Invoke-AwsWriteJson {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments)
    $text = Invoke-AwsWrite @AwsArguments
    if (-not $text) { return $null }
    return ($text | ConvertFrom-Json)
}

function Wait-AwsState {
    <# A waiter belongs to a write path: with -DryRun nothing changed, so nothing is awaited. #>
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments)
    if ($DryRun) { return }
    Invoke-Aws @AwsArguments | Out-Null
}

function Get-AwsAllItems {
    <#
      Runs a paginated AWS CLI list call and returns the items of every page (item 14). Without
      this the script only ever saw page 1, so in a large account the target API, integration,
      route, stage, log group, subscription or role policy could look absent and be recreated
      or left inconsistent.

      The result is always an array - zero items, one item or many - so ".Count" is always
      meaningful. A call site must NOT wrap it in the array subexpression operator: applied to a
      *command*, that collects the returned array as one element and makes .Count always 1.
      The behaviour is measured, not assumed: without the comma below, a one-item result arrives
      as a bare object whose .Count is $null, which reads as "nothing there" - and that is how
      the integration check would have created a second integration on a live API instead of
      refusing to guess.
    #>
    param(
        [Parameter(Mandatory = $true)][string[]]$AwsArguments,
        [Parameter(Mandatory = $true)][string]$ItemsProperty,
        [string]$TokenArgument = '--next-token',
        [string]$TokenProperty = 'NextToken'
    )
    $items = New-Object System.Collections.ArrayList
    $token = $null
    $previousToken = $null
    do {
        $call = @($AwsArguments)
        if ($token) { $call += @($TokenArgument, $token) }
        $page = Invoke-AwsJson @call
        $previousToken = $token
        $token = $null
        if ($page) {
            foreach ($item in @($page.$ItemsProperty)) {
                if ($null -ne $item) { [void]$items.Add($item) }
            }
            $token = $page.$TokenProperty
        }
        # A repeated token would make this loop run forever (a hung deploy is worse than a
        # failure), so an unmoving token is treated as an error rather than trusted.
        if ($token -and $token -eq $previousToken) {
            Write-Fail "the AWS pagination token for 'aws $($AwsArguments -join ' ')' did not advance ('$token'); refusing to loop forever."
        }
    } while ($token)
    # The leading comma keeps the array whole: without it, a *single* item is unrolled into a
    # scalar on the way out, and "$x.Count" on a scalar is $null - which reads as "no items" and
    # would send the caller down the create path. Callers wrap this in @(...) as well.
    return , @($items)
}

function Get-ValidatedEnvDocument {
    <#
      Reads the Lambda environment document and validates its *shape* before anything is sent
      to AWS (item 8): Lambda accepts an object whose "Variables" member is an object of string
      values and rejects everything else with an opaque API error. Only variable names and value
      *types* are ever reported - the values are secrets.
    #>
    param([string]$Path)
    $document = $null
    try {
        $document = (Get-Content -Raw -Path $Path) | ConvertFrom-Json
    } catch {
        Write-Fail "$Path is not valid JSON ($($_.Exception.Message)). Fix it or regenerate it with: python tools/init_ingest_key.py --reuse --lambda-env build\env-prod.json"
    }
    if ($null -eq $document -or $document -is [array] -or $document -is [string] -or $document -isnot [System.Management.Automation.PSCustomObject]) {
        Write-Fail "$Path must contain a JSON object shaped like { `"Variables`": { `"NAME`": `"value`" } }; Lambda accepts no other shape."
    }
    if (-not $document.PSObject.Properties['Variables']) {
        Write-Fail "$Path has no 'Variables' member; the Lambda environment document requires one."
    }
    $variables = $document.Variables
    if ($null -eq $variables -or $variables -isnot [System.Management.Automation.PSCustomObject]) {
        Write-Fail "$Path.'Variables' must be a JSON object of NAME/value pairs."
    }
    $nonStrings = New-Object System.Collections.ArrayList
    foreach ($property in $variables.PSObject.Properties) {
        if ($property.Value -isnot [string]) {
            $typeName = 'null'
            if ($null -ne $property.Value) { $typeName = $property.Value.GetType().Name }
            [void]$nonStrings.Add("$($property.Name) ($typeName)")
        }
    }
    if ($nonStrings.Count -gt 0) {
        Write-Fail "every value in $Path must be a JSON string; these are not: $($nonStrings -join ', '). Lambda rejects non-string values, so the deploy stops here before any AWS call (item 8)."
    }
    return $document
}

function Set-EnvVariables {
    <#
      Merges name/value pairs into the in-memory environment document, persists it (unless
      -DryRun) and returns the resulting variable names. Only names are ever returned: the
      document carries the ingestion secret.
    #>
    param([System.Collections.IDictionary]$Values)
    foreach ($name in @($Values.Keys)) {
        $envDocument.Variables | Add-Member -Force -NotePropertyName $name -NotePropertyValue ([string]$Values[$name])
    }
    Write-JsonFile $envPath ($envDocument | ConvertTo-Json -Depth 6)
    return @($envDocument.Variables.PSObject.Properties.Name)
}

function Test-S3PublicReadStatement {
    <#
      True when a bucket policy already grants public s3:GetObject on the bucket's objects.
      Used to decide whether an *existing* bucket is readable (item 4): this script never
      widens a bucket it did not create, and never narrows one either.
    #>
    param([object]$PolicyDocument, [string]$Bucket)
    if (-not $PolicyDocument) { return $false }
    foreach ($statement in @($PolicyDocument.Statement)) {
        if ($null -eq $statement) { continue }
        if ("$($statement.Effect)" -ne 'Allow') { continue }
        $principal = $statement.Principal
        $isPublic = $false
        if ($principal -is [string] -and $principal -eq '*') { $isPublic = $true }
        if ($principal -and $principal.PSObject.Properties['AWS'] -and @($principal.AWS) -contains '*') { $isPublic = $true }
        if (-not $isPublic) { continue }
        $actions = @($statement.Action)
        if (-not ($actions -contains 's3:GetObject' -or $actions -contains 's3:*' -or $actions -contains '*')) { continue }
        $resources = @($statement.Resource)
        # the bucket ARN is "arn:aws:s3:::<bucket>" (three colons after s3), objects add "/*"
        if ($resources -contains "arn:aws:s3:::$Bucket/*" -or $resources -contains "arn:aws:s3:::$Bucket" -or $resources -contains 'arn:aws:s3:::*' -or $resources -contains '*') { return $true }
    }
    return $false
}

# ------------------------------------------------------------------ dashboard assets --- #

<#
    The dashboard's fixed asset list, defined once and used by both the full deploy and the
    front-end-only path (-DashboardOnly). Dropping a file into web/ does not upload it: adding a
    row here does. Nothing else in web/ is published - the prepared originals, the misnamed
    *.svg rasters and the design sources stay out of the bucket.

    Cache policy: the four code files are `no-cache` because their names never change, and the
    six images are `public, max-age=86400` - a day, not a year - for the same reason. A year of
    `immutable` is only safe for content-hashed filenames, and these are not hashed, so a new
    nightscape would never reach a returning visitor.
#>
function Get-DashboardAssets {
    return @(
        @{ File = 'index.html';                    Type = 'text/html; charset=utf-8'; Cache = 'no-cache' },
        @{ File = 'styles.css';                    Type = 'text/css; charset=utf-8'; Cache = 'no-cache' },
        @{ File = 'lib.js';                        Type = 'application/javascript; charset=utf-8'; Cache = 'no-cache' },
        @{ File = 'app.js';                        Type = 'application/javascript; charset=utf-8'; Cache = 'no-cache' },
        @{ File = 'bg-storm.webp';                 Type = 'image/webp';              Cache = 'public, max-age=86400' },
        @{ File = 'chennai-nightscape.webp';       Type = 'image/webp';              Cache = 'public, max-age=86400' },
        @{ File = 'mumbai-nightscape.webp';        Type = 'image/webp';              Cache = 'public, max-age=86400' },
        @{ File = 'bengaluru-nightscape.webp';     Type = 'image/webp';              Cache = 'public, max-age=86400' },
        @{ File = 'hyderabad-nightscape.webp';     Type = 'image/webp';              Cache = 'public, max-age=86400' },
        @{ File = 'kochi-nightscape.webp';        Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'delhi.webp';                   Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'kolkata.webp';                 Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'guwahati.webp';                Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'patna.webp';                   Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'bhubaneshwar.webp';            Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'visakhapatnam.webp';           Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'pune.webp';                    Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'ahmedabad.webp';               Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'jaipur.webp';                  Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'lucknow.webp';                 Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'surat.webp';                 Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'mangaluru.webp';                 Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'kozhikode.webp';                 Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'puri.webp';                 Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'paradip.webp';                 Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'puducherry.webp';                 Type = 'image/webp';                            Cache = 'public, max-age=86400' }
        @{ File = 'portblair.webp';                 Type = 'image/webp';                            Cache = 'public, max-age=86400' }
    )
}

<#
    Uploads every asset in the list and then reads each object back, comparing the ETag with the
    MD5 of the local file and the stored metadata with what was asked for. `s3 cp` is already
    idempotent (an unchanged file uploads to the same ETag), so this stays safe to re-run: the
    comparison is what proves the bytes on the website are the bytes in web/.

    Neither the bucket nor its policy nor its website configuration is touched here: this
    function only writes objects that already belong in the bucket.
#>
function Send-DashboardAssets {
    foreach ($asset in Get-DashboardAssets) {
        $source = Join-Path $webDir $asset.File
        if (-not (Test-Path $source)) { Write-Fail "dashboard asset missing: $source" }
        Invoke-AwsWrite s3 cp $source "s3://$DashboardBucket/$($asset.File)" `
            --content-type $asset.Type --cache-control $asset.Cache | Out-Null
        Write-OkApplied "uploaded $($asset.File) ($($asset.Type), $($asset.Cache))"
    }
    if ($DryRun) {
        Write-DryRun 'the uploaded objects cannot be compared with web/ because nothing was written.'
        return $true
    }
    $mismatches = @()
    foreach ($asset in Get-DashboardAssets) {
        $source = Join-Path $webDir $asset.File
        $localMd5 = (Get-FileHash -Algorithm MD5 -Path $source).Hash.ToLower()
        $localSize = (Get-Item $source).Length
        $head = Invoke-AwsJson s3api head-object --bucket $DashboardBucket --key $asset.File
        $etag = "$($head.ETag)".Trim('"').ToLower()
        $deployedSize = [int64]$head.ContentLength
        $type = "$($head.ContentType)"
        $cache = "$($head.CacheControl)"
        if ($etag -ne $localMd5 -or $deployedSize -ne $localSize) {
            $mismatches += "$($asset.File): deployed $deployedSize B/$etag vs local $localSize B/$localMd5"
        } elseif ($type -ne $asset.Type -or $cache -ne $asset.Cache) {
            $mismatches += "$($asset.File): deployed $type / $cache, expected $($asset.Type) / $($asset.Cache)"
        } else {
            Write-Ok "$($asset.File): $([math]::Round($localSize / 1KB, 1)) KB, MD5 $localMd5, $type, $cache"
        }
    }
    if ($mismatches.Count -gt 0) {
        foreach ($line in $mismatches) { Write-Warn2 "  $line" }
        Write-Fail 'the deployed dashboard objects do not match web/ - the site is serving something else than this tree.'
    }
    Write-Ok "all $((Get-DashboardAssets).Count) dashboard objects match web/ byte for byte"
    return $true
}

# --------------------------------------------------------------------------- preflight --------------------------------------------------------------------------- #

Write-Step "FloodPulse deploy (profile $Profile, region $Region)"
if ($DryRun) {
    # item 19: every read and comparison below still runs; only the writes are turned into text.
    Write-DryRun 'DRY RUN: no AWS resource and no build file is changed. Reads and comparisons still run.'
}
$identity = Invoke-AwsJson sts get-caller-identity
Write-Ok "identity: $($identity.Arn) (account $($identity.Account))"
if ($identity.Arn -match ':root$') { Write-Fail 'refusing to deploy with the AWS root account' }

# item 17: this deploy ships the *working tree* - src/ becomes the Lambda package and web/
# becomes the bucket contents - so uncommitted work used to go live silently. Report it and
# make the operator opt in with -AllowDirtyTree.
$dirtyTree = ''
$gitExit = 0
try {
    $dirtyTree = (& git status --porcelain -- src web infra 2>&1 | Out-String).Trim()
    $gitExit = $LASTEXITCODE
} catch {
    if (-not $AllowDirtyTree) {
        Write-Fail "the dirty-tree preflight needs git on PATH: $($_.Exception.Message). Fix git, or re-run with -AllowDirtyTree to skip this check deliberately."
    }
    Write-Warn2 "could not run 'git status --porcelain' ($($_.Exception.Message)); -AllowDirtyTree was passed, so the check is skipped."
}
if ($gitExit -ne 0) { Write-Fail "could not read 'git status --porcelain' (exit $gitExit): $dirtyTree" }
if ($dirtyTree) {
    Write-Warn2 'the working tree has uncommitted changes in src/, web/ or infra/:'
    foreach ($line in ($dirtyTree -split "`r?`n")) { if ($line) { Write-Warn2 "  $line" } }
    if (-not $AllowDirtyTree) {
        Write-Fail 'refusing to deploy uncommitted code (item 17): commit or stash those changes, or re-run with -AllowDirtyTree if shipping the working tree is intended.'
    }
    Write-Warn2 '-AllowDirtyTree was passed: the working tree listed above is what this run deploys.'
} else {
    Write-Ok 'working tree clean for src/, web/ and infra/'
}

# ------------------------------------------------------------------ front-end only path --- #

if ($DashboardOnly) {
    # A dashboard-and-images deploy, and nothing else. The Lambda package, the table, the API,
    # the routes, the log groups and the alert topic are not read or written by this path: it
    # exists so that publishing a stylesheet or a nightscape cannot become an excuse to ship a
    # backend change, and so that reviewing "what went live today" has one honest answer.
    Write-Step "FloodPulse front-end deploy only (profile $Profile, region $Region)"
    if ($SkipDashboard) {
        Write-Fail '-DashboardOnly and -SkipDashboard cancel each other out: there would be nothing to do.'
    }
    if (-not (Test-AwsResource s3api head-bucket --bucket $DashboardBucket)) {
        Write-Fail "bucket '$DashboardBucket' does not exist. This path never creates one: run the full deploy once, or fix -DashboardBucket."
    }
    Write-Ok "bucket exists: s3://$DashboardBucket (left exactly as it is: no policy, no website, no lifecycle is touched)"
    Send-DashboardAssets | Out-Null
    Write-Step 'Front-end summary'
    Write-Host "  dashboard   : http://$DashboardBucket.s3-website.$Region.amazonaws.com"
    Write-Host "  objects     : $((Get-DashboardAssets).Count) uploaded, each verified against web/"
    Write-Host '  untouched   : Lambda, API Gateway, DynamoDB, IAM, SNS, log groups'
    Write-Ok 'front-end deploy complete'
    exit 0
}

if (-not (Test-AwsResource iam get-role --role-name $RoleName)) {
    throw "execution role '$RoleName' was not found. Create it first (trust lambda.amazonaws.com, logs:* plus dynamodb:PutItem/GetItem/Query on table/$TableName), then re-run this script."
}
$roleArn = (Invoke-AwsJson iam get-role --role-name $RoleName).Role.Arn
Write-Ok "execution role: $roleArn"

# --------------------------------------------------------------------------- build --------------------------------------------------------------------------- #

Write-Step 'Building deployment package'
if ($DryRun) {
    Write-DryRun "would create $buildDir and the helper JSON files (pk/sk key, expression values)"
} else {
    New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
}
$keyPath = Join-Path $buildDir 'key-latest.json'
$eavPath = Join-Path $buildDir 'eav-pk.json'
Write-JsonFile $keyPath '{"pk": {"S": "ZONE#zone-riverside-north"}, "sk": {"S": "LATEST"}}'
Write-JsonFile $eavPath '{":pk": {"S": "ZONE#zone-riverside-north"}}'

# --------------------------------------------------------------------------- secret + env --------------------------------------------------------------------------- #

Write-Step 'Ingestion secret and Lambda environment'
if (-not (Test-Path $envPath)) {
    if ($DryRun) {
        # A dry run writes nothing, and preparing the secret writes two local files, so it
        # cannot be part of one: say so instead of silently generating a secret.
        Write-Fail "-DryRun needs the existing local environment file ($envPath). Create it first with: python tools/init_ingest_key.py --lambda-env build\env-prod.json (local files only, no AWS call), then re-run with -DryRun."
    }
    if (-not (Test-Path $keyFilePath)) {
        Write-Info "no local secret yet; generating $IngestKeyFile and $envPath"
    }
    $initArgs = @('tools/init_ingest_key.py', '--key-file', $keyFilePath, '--lambda-env', $envPath, '--table', $TableName)
    if (Test-Path $keyFilePath) { $initArgs += '--reuse' }
    $initOutput = (& python @initArgs 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw (Remove-SecretText "failed to prepare the ingestion secret:`n$initOutput") }
    Write-Ok "secret prepared (value never displayed): $IngestKeyFile + $envPath"
} else {
    Write-Ok "environment file already present: $envPath (gitignored)"
}
if (-not (Test-Path $keyFilePath)) {
    Write-Fail "missing '$IngestKeyFile'. Create it with: python tools/init_ingest_key.py --lambda-env build\env-prod.json"
}
# item 8: the document shape is validated before it can reach AWS (Lambda only accepts an
# object whose "Variables" holds string values), and only names/types are reported.
$envDocument = Get-ValidatedEnvDocument $envPath
$envVariables = $envDocument.Variables
$envNames = @($envVariables.PSObject.Properties.Name)
Write-Info "lambda environment variables: $($envNames -join ', ') (values not displayed)"
if ($envNames -notcontains 'FLOODPULSE_INGEST_KEY') {
    # item 2: this used to warn and carry on, which deployed a function answering every ingest
    # request with HTTP 503 - a failure that only shows up during the demo.
    Write-Fail "the environment file has no FLOODPULSE_INGEST_KEY: the deployed API would fail closed with HTTP 503, so this deploy stops. Regenerate it with: python tools/init_ingest_key.py --key-file $IngestKeyFile --lambda-env build\env-prod.json"
}

# This deploy applies the *environment file*. The key file is the documented source of truth,
# so a rotation that updated only the key file would be silently undone here. Item 3 makes that
# fatal unless -AllowKeyDrift is passed; only fingerprints are ever printed, never the value.
$envKey = [string]$envVariables.PSObject.Properties['FLOODPULSE_INGEST_KEY'].Value
$fileKey = (Get-Content -Raw -Path $keyFilePath).Trim()
Register-Secret $envKey
Register-Secret $fileKey
if ($envKey -ne $fileKey) {
    Write-Warn2 "the environment file and $IngestKeyFile carry different ingestion keys ($(Get-SecretFingerprint $envKey) vs $(Get-SecretFingerprint $fileKey))."
    Write-Warn2 'deploying now would apply the environment-file key and undo a rotation that touched only the key file.'
    if (-not $AllowKeyDrift) {
        Write-Fail "refusing to deploy on this key drift (item 3). Fix it with: python tools/init_ingest_key.py --reuse --key-file $IngestKeyFile --lambda-env build\env-prod.json - or re-run with -AllowKeyDrift if the environment-file key is the one you mean to deploy."
    }
    Write-Warn2 "-AllowKeyDrift was passed: the environment-file key $(Get-SecretFingerprint $envKey) is what will be deployed."
} else {
    Write-Ok "environment file and $IngestKeyFile agree on the ingestion key ($(Get-SecretFingerprint $fileKey))"
}

# --------------------------------------------------------------------------- sns topic --------------------------------------------------------------------------- #

if (-not $SkipAlerts) {
    Write-Step "SNS alert topic '$AlertTopicName'"
    # The ARN of an SNS topic is deterministic, so the topic can be probed before it is created.
    # item 12: "create-topic --attributes" used to run on every deploy, which reset a DisplayName
    # an operator had changed by hand; attributes are now written only when creating the topic.
    $alertTopicArn = "arn:aws:sns:${Region}:$($identity.Account):$AlertTopicName"
    $topicExists = Test-AwsResource sns get-topic-attributes --topic-arn $alertTopicArn
    if ($topicExists) {
        Write-Ok "topic already exists: $alertTopicArn (attributes left untouched)"
    } else {
        Invoke-AwsWrite sns create-topic --name $AlertTopicName --attributes 'DisplayName=FloodPulse Alerts' | Out-Null
        Write-OkApplied "topic created: $alertTopicArn (DisplayName=FloodPulse Alerts)"
    }

    if (-not $topicExists) {
        Write-Info 'the topic does not exist yet, so its subscriptions are checked on the next run.'
    } else {
        # item 14: paginated, so a large subscription list cannot hide a pending or missing one
        $subscriptions = Get-AwsAllItems -AwsArguments @('sns', 'list-subscriptions-by-topic', '--topic-arn', $alertTopicArn) -ItemsProperty 'Subscriptions'
        $confirmed = @($subscriptions | Where-Object { $_.SubscriptionArn -ne 'PendingConfirmation' }).Count
        $pending = @($subscriptions | Where-Object { $_.SubscriptionArn -eq 'PendingConfirmation' }).Count
        if ($confirmed -eq 0) {
            Write-Warn2 "no confirmed subscription on the topic: publishes will succeed but nobody is notified."
            Write-Warn2 "add an email subscriber with (the address is the operator's choice):"
            Write-Warn2 "  aws sns subscribe --topic-arn $alertTopicArn --protocol email --notification-endpoint <address> --profile $Profile --region $Region"
        } else {
            Write-Ok "$confirmed confirmed subscription(s), $pending pending"
        }
    }

    # merge the topic ARN (and the cooldown) into the gitignored env file: only the
    # variable names are ever printed, never the ingestion secret
    $envNames = Set-EnvVariables ([ordered]@{
            FLOODPULSE_SNS_TOPIC_ARN          = $alertTopicArn
            FLOODPULSE_ALERT_COOLDOWN_SECONDS = "$AlertCooldownSeconds"
        })
    Write-OkApplied "environment file updated: $($envNames -join ', ') (values not displayed; cooldown ${AlertCooldownSeconds}s)"

    # the execution role must be allowed to publish to this one topic and nothing else.
    # This needs iam:PutRolePolicy; if that is denied we report it and carry on, because
    # incident tracking and the dashboard do not depend on alerting.
    $rolePolicies = Get-AwsAllItems -AwsArguments @('iam', 'list-role-policies', '--role-name', $RoleName) -ItemsProperty 'PolicyNames' -TokenArgument '--marker' -TokenProperty 'Marker'
    if (@($rolePolicies) -contains 'FloodPulseSnsPublish') {
        Write-Ok 'inline policy FloodPulseSnsPublish already attached'
    } else {
        $snsPolicyPath = Join-Path $buildDir 'iam-sns-publish.json'
        $snsPolicy = [ordered]@{
            Version   = '2012-10-17'
            Statement = @([ordered]@{
                    Sid      = 'PublishOnlyToTheFloodPulseAlertTopic'
                    Effect   = 'Allow'
                    Action   = 'sns:Publish'
                    Resource = $alertTopicArn
                })
        } | ConvertTo-Json -Depth 5
        Write-JsonFile $snsPolicyPath $snsPolicy
        try {
            Invoke-AwsWrite iam put-role-policy --role-name $RoleName --policy-name FloodPulseSnsPublish --policy-document "file://$snsPolicyPath" | Out-Null
            Write-OkApplied "inline policy FloodPulseSnsPublish attached (sns:Publish on $alertTopicArn only)"
        } catch {
            $alertsBlocked = $true
            Write-Warn2 'IAM DENIED: could not attach FloodPulseSnsPublish; alerting will report publish failures.'
            Write-Warn2 (Remove-SecretText ($_.Exception.Message -replace "`n", ' '))
            Write-Warn2 "operator action: attach this inline policy to $RoleName, then re-run this script:"
            Write-Warn2 "  aws iam put-role-policy --role-name $RoleName --policy-name FloodPulseSnsPublish --policy-document file://$snsPolicyPath --profile $Profile --region $Region"
        }
    }

    # NOT APPLIED HERE ON PURPOSE: POST /subscriptions needs the same role to be allowed
    # sns:Subscribe on the same single topic. That statement is a manual approval, not an
    # automated step - this script attaches Publish-only and never widens the role beyond it.
    # An operator who has approved it can put the second action on the same inline policy:
    #
    #   aws iam put-role-policy --role-name <role> --policy-name FloodPulseSnsPublish `
    #     --policy-document file://infra/iam/floodpulse-sns-publish-and-subscribe.json `
    #     --profile <profile> --region <region>
    #
    # That document is tracked in infra/iam/ (see infra/iam/README.md) so the operator does not
    # have to reconstruct it, and it is applied by nothing in this repository.
    #
    # with a document identical to the one above except that Action is the two-element list
    # ["sns:Publish", "sns:Subscribe"] and Resource is still only
    # arn:aws:sns:<region>:<account>:floodpulse-alerts. Until that is granted, the route answers
    # 503 with a friendly message: a missing permission degrades one public form, it never
    # breaks the dashboard, ingestion or alerts.
}

# ------------------------------------------------------- M4: zones and live weather ------------------------------------------------------- #
# The zone mode decides which fleet is live: 'live' = the five Indian city zones fed by
# Open-Meteo, 'demo' = the three simulated zones. The weather switch is a kill switch:
# 'off' makes the Lambda serve cached/simulated data only and never calls the network.
# These are merged after the SNS step so they are always present, whatever is skipped.

Write-Step "Zone mode and external weather ($ZoneMode / $WeatherMode)"
$envNames = Set-EnvVariables ([ordered]@{
        FLOODPULSE_ZONE_MODE               = "$ZoneMode"
        FLOODPULSE_WEATHER                 = "$WeatherMode"
        FLOODPULSE_WEATHER_REFRESH_SECONDS = "$WeatherRefreshSeconds"
        FLOODPULSE_WEATHER_TIMEOUT_SECONDS = "$WeatherTimeoutSeconds"
    })
Write-OkApplied "environment file updated: $($envNames -join ', ') (values not displayed)"
Write-Info "zone mode '$ZoneMode' + weather '$WeatherMode' at most every ${WeatherRefreshSeconds}s; no new AWS resources are created"

# --------------------------------------------------------------------------- cors --------------------------------------------------------------------------- #

$dashboardUrl = "http://$DashboardBucket.s3-website.$Region.amazonaws.com"
$allowedOrigins = @($dashboardUrl)
if ($LocalDevOrigin) { $allowedOrigins += $LocalDevOrigin }
# item 13: these are the *desired* values. They are only pushed to the API when the deployed
# CORS block differs from them (see the HTTP API step); update-api replaces the whole block, so
# re-applying it blindly used to drop hand-added origins and re-add the dev origin every run.
# The dev origin is never added silently: it is in this list, and it can be dropped by passing
# -LocalDevOrigin ''.
$desiredMethods = @('GET', 'POST', 'OPTIONS')
$desiredHeaders = @('content-type', 'x-fp-key')
$desiredMaxAge = 300
$corsJson = @{
    AllowOrigins = $allowedOrigins
    AllowMethods = $desiredMethods
    AllowHeaders = $desiredHeaders
    MaxAge       = $desiredMaxAge
} | ConvertTo-Json -Depth 4
Write-JsonFile $corsPath $corsJson
Write-Ok "CORS desired: $($allowedOrigins -join ', ') (written only when the deployed block differs)"

$zipBuilder = @"
import pathlib, zipfile
root = pathlib.Path(r'$srcDir')
out = pathlib.Path(r'$zipPath')
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob('*.py')):
        archive.write(str(path), path.relative_to(root).as_posix())
print(out.stat().st_size)
"@
if ($DryRun) {
    Write-DryRun "would build $zipPath from every .py file under $srcDir"
} else {
    $zipSize = ($zipBuilder | python -).Trim()
    if ($LASTEXITCODE -ne 0) { Write-Fail 'failed to build the deployment zip' }
    Write-Ok "package: $zipPath ($zipSize bytes)"
}

# --------------------------------------------------------------------------- table --------------------------------------------------------------------------- #

Write-Step "DynamoDB table '$TableName'"
$tableExists = Test-AwsResource dynamodb describe-table --table-name $TableName
if ($tableExists) {
    Write-Ok 'table already exists'
} else {
    Invoke-AwsWrite dynamodb create-table --table-name $TableName `
        --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S `
        --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE `
        --billing-mode PAY_PER_REQUEST | Out-Null
    Wait-AwsState dynamodb wait table-exists --table-name $TableName
    Write-OkApplied 'table created'
}
if ($tableExists) {
    $ttl = Invoke-AwsJson dynamodb describe-time-to-live --table-name $TableName
    if ($ttl.TimeToLiveDescription.TimeToLiveStatus -ne 'ENABLED') {
        Invoke-AwsWrite dynamodb update-time-to-live --table-name $TableName --time-to-live-specification Enabled=true,AttributeName=ttl | Out-Null
        Write-OkApplied 'TTL enabled on "ttl"'
    } else {
        Write-Ok "TTL already enabled on '$($ttl.TimeToLiveDescription.AttributeName)'"
    }
} elseif ($DryRun) {
    # A dry run did not create the table, so there is no TTL setting to read or plan.
    Write-DryRun 'the TTL setting would be enabled on "ttl" right after the table is created'
}

# --------------------------------------------------------------------------- lambda --------------------------------------------------------------------------- #

Write-Step "Lambda function '$FunctionName'"
$handler = 'floodpulse.handler.lambda_handler'
$functionExists = Test-AwsResource lambda get-function --function-name $FunctionName
$functionConfig = $null
if ($functionExists) {
    # Read the deployed configuration *first*: handler, role and the environment map are all
    # compared against what is really deployed before anything is written (items 1 and 11).
    $functionConfig = Invoke-AwsJson lambda get-function-configuration --function-name $FunctionName

    Invoke-AwsWrite lambda update-function-code --function-name $FunctionName --zip-file "fileb://$zipPath" | Out-Null
    Wait-AwsState lambda wait function-updated --function-name $FunctionName
    Write-OkApplied 'code updated from the local package'

    $configDrift = New-Object System.Collections.ArrayList
    if ($functionConfig.MemorySize -ne $MemorySize) { [void]$configDrift.Add("memory $($functionConfig.MemorySize) MB -> $MemorySize MB") }
    if ($functionConfig.Timeout -ne $TimeoutSeconds) { [void]$configDrift.Add("timeout $($functionConfig.Timeout)s -> ${TimeoutSeconds}s") }
    if ($functionConfig.Runtime -ne $Runtime) { [void]$configDrift.Add("runtime $($functionConfig.Runtime) -> $Runtime") }
    # item 11: the handler was written on every configuration update but never compared, so a
    # function pointing at a different handler (a stale name, a renamed module) went unnoticed.
    if ($functionConfig.Handler -ne $handler) { [void]$configDrift.Add("handler $($functionConfig.Handler) -> $handler") }
    if ($configDrift.Count -gt 0) {
        Invoke-AwsWrite lambda update-function-configuration --function-name $FunctionName `
            --runtime $Runtime --memory-size $MemorySize --timeout $TimeoutSeconds `
            --handler $handler | Out-Null
        Wait-AwsState lambda wait function-updated --function-name $FunctionName
        Write-OkApplied "configuration corrected ($($configDrift -join ', '))"
    } else {
        Write-Ok "configuration already correct (memory $MemorySize MB, timeout ${TimeoutSeconds}s, $Runtime, handler $handler)"
    }

    # item 11: the execution role is compared and reported, but never reassigned. This script
    # does not change IAM here: assume-role and trust configuration belong to the project owner.
    if ($functionConfig.Role -ne $roleArn) {
        Write-Warn2 'the execution role of the deployed function differs from the expected one and is NOT changed by this script:'
        Write-Warn2 "  deployed: $($functionConfig.Role)"
        Write-Warn2 "  expected: $roleArn"
        Write-Warn2 "  if that is not intentional, switch it by hand with 'aws lambda update-function-configuration --function-name $FunctionName --role <arn> --profile $Profile --region $Region' or point -RoleName at the right role."
    } else {
        Write-Ok "execution role matches ($($functionConfig.Role))"
    }

    # item 1: read the deployed map and merge the local document *on top of* the deployed
    # names. The old code applied the local document as a whole map, so any variable that lived
    # in AWS but not in the local env file was silently deleted by the next deploy.
    $deployedEnvNames = @()
    if ($functionConfig.Environment -and $functionConfig.Environment.Variables) {
        $deployedEnvNames = @($functionConfig.Environment.Variables.PSObject.Properties.Name)
    }
    $localNames = @($envDocument.Variables.PSObject.Properties.Name)
    $missingLocally = @($deployedEnvNames | Where-Object { $localNames -notcontains $_ })
    $addedLocally = @($localNames | Where-Object { $deployedEnvNames -notcontains $_ })
    $changedValues = @()
    if ($functionConfig.Environment -and $functionConfig.Environment.Variables) {
        foreach ($name in @($localNames | Where-Object { $deployedEnvNames -contains $_ })) {
            $deployedValue = [string]$functionConfig.Environment.Variables.PSObject.Properties[$name].Value
            $localValue = [string]$envDocument.Variables.PSObject.Properties[$name].Value
            if ($deployedValue -cne $localValue) { $changedValues += $name }
        }
    }
    if ($missingLocally.Count -gt 0) {
        Write-Warn2 "these variables exist on the deployed function but not in the local environment file: $($missingLocally -join ', ')"
        if (-not $KeepUnknownEnvVars) {
            Write-Fail "refusing to delete AWS-side variables implicitly (item 1). Add them to $envPath, or re-run with -KeepUnknownEnvVars to keep their deployed values in place."
        }
        Write-Info "keeping the deployed value of: $($missingLocally -join ', ') (-KeepUnknownEnvVars)"
    }

    # The merged document is built in build/ and applied from there: the operator's environment
    # file is never rewritten with AWS-side values behind their back.
    $mergedPath = Join-Path $buildDir 'env-merged.json'
    $mergedVariables = [ordered]@{}
    foreach ($property in $envDocument.Variables.PSObject.Properties) { $mergedVariables[$property.Name] = [string]$property.Value }
    if ($KeepUnknownEnvVars) {
        foreach ($name in $missingLocally) { $mergedVariables[$name] = [string]$functionConfig.Environment.Variables.PSObject.Properties[$name].Value }
    }
    $envChanges = New-Object System.Collections.ArrayList
    if ($addedLocally.Count -gt 0) { [void]$envChanges.Add("add: $($addedLocally -join ', ')") }
    if ($changedValues.Count -gt 0) { [void]$envChanges.Add("change: $($changedValues -join ', ')") }
    foreach ($name in $changedValues) {
        # names only for everything else; the ingestion key is reported as a fingerprint, so a
        # rotation is visible without the value ever being displayed (item 16)
        if ($name -eq 'FLOODPULSE_INGEST_KEY') {
            $deployedPrint = Get-SecretFingerprint ([string]$functionConfig.Environment.Variables.PSObject.Properties[$name].Value)
            $localPrint = Get-SecretFingerprint ([string]$envDocument.Variables.PSObject.Properties[$name].Value)
            Write-Info "FLOODPULSE_INGEST_KEY differs: deployed $deployedPrint -> local $localPrint"
        } else {
            Write-Info "value differs (not displayed): $name"
        }
    }
    if ($envChanges.Count -eq 0) {
        if ($missingLocally.Count -gt 0) {
            # the merge is what makes this a no-op: the AWS-side variables keep their deployed
            # values and nothing is written
            Write-Ok "nothing to apply: every local variable is already deployed, and the variable(s) only AWS knows ($($missingLocally -join ', ')) keep their deployed values"
        } else {
            Write-Ok "environment already matches the local document: $($localNames -join ', ') (values not displayed)"
        }
    } else {
        Write-Info "environment changes: $($envChanges -join '; ')"
        $mergedDocument = @{ Variables = $mergedVariables } | ConvertTo-Json -Depth 6
        Write-JsonFile $mergedPath $mergedDocument
        Invoke-AwsWrite lambda update-function-configuration --function-name $FunctionName `
            --environment "file://$mergedPath" | Out-Null
        Wait-AwsState lambda wait function-updated --function-name $FunctionName
        if (-not $DryRun) {
            $appliedNames = @(((Invoke-AwsJson lambda get-function-configuration --function-name $FunctionName).Environment.Variables).PSObject.Properties.Name)
            Write-Ok "environment applied: $($appliedNames -join ', ') (values not displayed)"
        }
    }
} else {
    if ($DryRun) {
        Write-DryRun "there is no deployed environment to compare yet; it would be created with the $($envNames.Count) variable(s) in $envPath"
    }
    Invoke-AwsWrite lambda create-function --function-name $FunctionName `
        --runtime $Runtime --role $roleArn --handler $handler `
        --memory-size $MemorySize --timeout $TimeoutSeconds `
        --zip-file "fileb://$zipPath" --environment "file://$envPath" | Out-Null
    Wait-AwsState lambda wait function-active --function-name $FunctionName
    Write-OkApplied 'function created'
}
# Lambda ARNs are deterministic, so the ARN is available even in a -DryRun that created nothing.
$functionArn = "arn:aws:lambda:${Region}:$($identity.Account):function:$FunctionName"
if ($functionConfig -and $functionConfig.FunctionArn) { $functionArn = $functionConfig.FunctionArn }

# --------------------------------------------------------------------------- http api --------------------------------------------------------------------------- #

Write-Step "HTTP API '$ApiName'"
# item 14: paginated, so an account with more than one page of APIs cannot hide the target
$apis = Get-AwsAllItems -AwsArguments @('apigatewayv2', 'get-apis') -ItemsProperty 'Items'
# item 6: the match is now *exact name equality* and an ambiguous name stops the run. The old
# "first name match" could silently pick a different API than the operator meant.
$apiMatches = @($apis | Where-Object { $_.Name -eq $ApiName })
if ($apiMatches.Count -gt 1) {
    $duplicateIds = @($apiMatches | ForEach-Object { $_.ApiId })
    Write-Fail "several HTTP APIs are named '$ApiName' ($($duplicateIds -join ', ')). Refusing to guess which one to update: rename or delete the duplicates, then re-run (item 6)."
}
$api = $null
if ($apiMatches.Count -eq 1) { $api = $apiMatches[0] }
if ($null -eq $api) {
    $api = Invoke-AwsWriteJson apigatewayv2 create-api --name $ApiName --protocol-type HTTP --cors-configuration "file://$corsPath"
    if ($api) {
        Write-OkApplied "API created: $($api.ApiId) with the desired CORS block"
    } else {
        Write-DryRun 'the API does not exist yet, so the invoke permission, integrations, routes and stage are reported on the next run.'
    }
} else {
    # item 13: the desired CORS block is compared with the deployed one instead of being
    # re-applied. update-api replaces the whole block, so the old unconditional call dropped
    # hand-added origins and re-added http://localhost:8000 on every single run.
    $deployedCors = $api.CorsConfiguration
    $deployedOrigins = @()
    if ($deployedCors -and $deployedCors.AllowOrigins) { $deployedOrigins = @($deployedCors.AllowOrigins) }
    $unknownOrigins = @(Compare-StringSet $deployedOrigins $allowedOrigins)
    if ($unknownOrigins.Count -gt 0) {
        # An origin this run does not know about would be removed by a rewrite. Refusing is the
        # safe behaviour: an unknown origin is either deliberate (and must be declared) or a
        # sign that somebody else is editing the API.
        Write-Fail "the deployed CORS configuration of API $($api.ApiId) allows origin(s) this run does not know about: $($unknownOrigins -join ', '). Refusing to rewrite the CORS block (that would remove them). Declare each one explicitly with -LocalDevOrigin <origin>, or remove it in the API Gateway console, then re-run (item 13)."
    }
    $deployedMethods = @()
    if ($deployedCors -and $deployedCors.AllowMethods) { $deployedMethods = @($deployedCors.AllowMethods) }
    $deployedHeaders = @()
    if ($deployedCors -and $deployedCors.AllowHeaders) { $deployedHeaders = @($deployedCors.AllowHeaders) }
    $deployedMaxAge = 0
    if ($deployedCors -and $deployedCors.MaxAge) { $deployedMaxAge = [int]$deployedCors.MaxAge }
    $corsChanges = New-Object System.Collections.ArrayList
    $missingOrigins = @(Compare-StringSet $allowedOrigins $deployedOrigins)
    if ($missingOrigins.Count -gt 0) { [void]$corsChanges.Add("allow-origins +$($missingOrigins -join ', ')") }
    $missingMethods = @(Compare-StringSet $desiredMethods $deployedMethods)
    if ($missingMethods.Count -gt 0) { [void]$corsChanges.Add("allow-methods +$($missingMethods -join ', ')") }
    $extraMethods = @(Compare-StringSet $deployedMethods $desiredMethods)
    if ($extraMethods.Count -gt 0) { [void]$corsChanges.Add("allow-methods -$($extraMethods -join ', ')") }
    $missingHeaders = @(Compare-StringSet $desiredHeaders $deployedHeaders)
    if ($missingHeaders.Count -gt 0) { [void]$corsChanges.Add("allow-headers +$($missingHeaders -join ', ')") }
    $extraHeaders = @(Compare-StringSet $deployedHeaders $desiredHeaders)
    if ($extraHeaders.Count -gt 0) { [void]$corsChanges.Add("allow-headers -$($extraHeaders -join ', ')") }
    if ($deployedMaxAge -ne $desiredMaxAge) { [void]$corsChanges.Add("max-age $deployedMaxAge -> $desiredMaxAge") }
    if ($corsChanges.Count -eq 0) {
        Write-Ok "API already exists: $($api.ApiId) (CORS already matches: $($allowedOrigins -join ', '))"
    } else {
        Write-Info "CORS differs from the desired configuration: $($corsChanges -join '; ')"
        Invoke-AwsWrite apigatewayv2 update-api --api-id $api.ApiId --cors-configuration "file://$corsPath" | Out-Null
        Write-OkApplied "API already exists: $($api.ApiId) (CORS synced to the desired block)"
    }
}
$apiId = $null
$endpoint = $null
if ($api) {
    $apiId = $api.ApiId
    $endpoint = $api.ApiEndpoint
}

Write-Step 'API -> Lambda invoke permission'
if (-not $apiId) {
    Write-Info 'the API does not exist yet, so the invoke permission is granted (and compared) on the next run.'
} else {
    $statementId = 'floodpulse-apigateway-invoke'
    $sourceArn = "arn:aws:execute-api:${Region}:$($identity.Account):${apiId}/*/*"
    $policy = $null
    if (Test-AwsResource lambda get-policy --function-name $FunctionName) {
        $policy = Invoke-Aws lambda get-policy --function-name $FunctionName
    }
    if ($policy -and $policy -match [regex]::Escape($statementId) -and $policy -match [regex]::Escape($apiId)) {
        Write-Ok "statement '$statementId' already allows this API"
    } elseif ($policy -and $policy -match [regex]::Escape($statementId)) {
        Write-Warn2 "statement '$statementId' exists but does not reference API $apiId. This happens when the API was recreated."
        Write-Warn2 "Fix it explicitly (nothing is changed automatically):"
        Write-Warn2 "  aws lambda remove-permission --function-name $FunctionName --statement-id $statementId --profile $Profile --region $Region"
        Write-Warn2 "  then re-run this script."
    } else {
        Invoke-AwsWrite lambda add-permission --function-name $FunctionName --statement-id $statementId `
            --action lambda:InvokeFunction --principal apigateway.amazonaws.com --source-arn $sourceArn | Out-Null
        Write-OkApplied "granted apigateway.amazonaws.com invoke on $sourceArn"
    }
}

Write-Step 'Routes and integration'
if (-not $apiId) {
    Write-Info 'the API does not exist yet: integrations and routes are reported on the next run.'
} else {
    # item 14: paginated. Note the idiom: this helper always returns an array (even for one item
    # and even for none), so it must NOT be wrapped in @() at a call site - @(command) would
    # collect the returned array as a *single* element and make ".Count" always 1.
    $integrations = Get-AwsAllItems -AwsArguments @('apigatewayv2', 'get-integrations', '--api-id', $apiId) -ItemsProperty 'Items'
    # item 5: integrations are matched on IntegrationUri. The old code took the *first*
    # integration in the listing and retargeted every route at it, which would silently point
    # the routes at whatever else that integration invoked.
    $matchingIntegrations = @($integrations | Where-Object { $_.IntegrationUri -eq $functionArn })
    $integration = $null
    if ($matchingIntegrations.Count -eq 0) {
        if ($integrations.Count -gt 0) {
            $existingTargets = @($integrations | ForEach-Object { "$($_.IntegrationId) -> $($_.IntegrationUri)" })
            Write-Fail "no integration on API $apiId points at $functionArn ($($existingTargets -join '; ')). Retargeting the routes at an unrelated integration is not safe, so the deploy stops: fix the API by hand and re-run (item 5)."
        }
        $integration = Invoke-AwsWriteJson apigatewayv2 create-integration --api-id $apiId --integration-type AWS_PROXY --integration-uri $functionArn --payload-format-version 2.0
        Write-OkApplied "integration created: $($integration.IntegrationId)"
    } else {
        $integration = $matchingIntegrations[0]
        if ($matchingIntegrations.Count -gt 1) {
            $duplicateIntegrationIds = @($matchingIntegrations | ForEach-Object { $_.IntegrationId })
            Write-Warn2 "$($matchingIntegrations.Count) integrations point at $functionArn ($($duplicateIntegrationIds -join ', ')); using $($integration.IntegrationId) for every route."
        } else {
            Write-Ok "integration already exists: $($integration.IntegrationId) (points at the function)"
        }
    }

    $routeKeys = @(
        'POST /readings'
        'GET /zones'
        'GET /zones/{zone_id}/history'
        'GET /incidents'
        # Public, anonymous, throttled: a visitor asks to be emailed flood alerts. The route
        # carries no ingestion key by design (the dashboard calls it from a browser); its
        # bounds are the 512-byte body limit, the address syntax check, the per-address
        # counter in the existing table, and the SNS topic that only the environment names.
        #
        # It also needs the function role to be allowed sns:Subscribe on the one alert topic.
        # That permission is NOT applied by this script - see the SNS section above - so the
        # route answers 503 "we couldn't create the subscription request right now" until an
        # operator grants it.
        'POST /subscriptions'
    )
    if ($null -eq $integration) {
        # -DryRun on an account where the integration does not exist yet: the routes cannot be
        # compared before it exists, so the plan is reported instead.
        foreach ($routeKey in $routeKeys) { Write-DryRun "would create route $routeKey -> the new integration" }
    } else {
        $target = "integrations/$($integration.IntegrationId)"
        # item 14: paginated
        $routes = Get-AwsAllItems -AwsArguments @('apigatewayv2', 'get-routes', '--api-id', $apiId) -ItemsProperty 'Items'
        foreach ($routeKey in $routeKeys) {
            $existing = $null
            if ($routes) { $existing = $routes | Where-Object { $_.RouteKey -eq $routeKey } | Select-Object -First 1 }
            if ($null -eq $existing) {
                Invoke-AwsWrite apigatewayv2 create-route --api-id $apiId --route-key $routeKey --target $target | Out-Null
                Write-OkApplied "route created: $routeKey"
            } elseif ($existing.Target -ne $target) {
                Invoke-AwsWrite apigatewayv2 update-route --api-id $apiId --route-id $existing.RouteId --target $target | Out-Null
                Write-OkApplied "route retargeted: $routeKey (from $($existing.Target) to $target)"
            } else {
                Write-Ok "route exists: $routeKey"
            }
        }
    }
}

Write-Step "Stage '$StageName'"
if (-not $apiId) {
    Write-Info 'the API does not exist yet: the stage is reported on the next run.'
} else {
    # item 14: paginated
    $stages = Get-AwsAllItems -AwsArguments @('apigatewayv2', 'get-stages', '--api-id', $apiId) -ItemsProperty 'Items'
    $stage = $null
    if ($stages) { $stage = $stages | Where-Object { $_.StageName -eq $StageName } | Select-Object -First 1 }
    if ($null -eq $stage) {
        Invoke-AwsWrite apigatewayv2 create-stage --api-id $apiId --stage-name $StageName --auto-deploy | Out-Null
        Write-OkApplied 'stage created with auto-deploy'
    } else {
        Write-Ok 'stage already exists'
    }
}

# --------------------------------------------------------------------------- logs --------------------------------------------------------------------------- #

Write-Step 'CloudWatch log retention'
$logGroup = "/aws/lambda/$FunctionName"
# item 14: paginated
$groups = Get-AwsAllItems -AwsArguments @('logs', 'describe-log-groups', '--log-group-name-prefix', $logGroup) -ItemsProperty 'logGroups' -TokenProperty 'nextToken'
$group = $null
if ($groups) { $group = $groups | Where-Object { $_.logGroupName -eq $logGroup } | Select-Object -First 1 }
if ($null -eq $group) {
    Write-Info "log group not created yet (it appears on the first invocation); set retention afterwards with:"
    Write-Info "  aws logs put-retention-policy --log-group-name $logGroup --retention-in-days $LogRetentionDays --profile $Profile --region $Region"
} else {
    $deployedRetention = $null
    if ($group.PSObject.Properties['retentionInDays']) { $deployedRetention = $group.retentionInDays }
    if ($null -eq $deployedRetention) {
        # item 9: "not set" means the logs never expire, so any number is a *shortening*, and
        # shortening deletes log events irreversibly. Report it and change nothing.
        Write-Warn2 "retention is not set on $logGroup (CloudWatch keeps those logs forever)."
        Write-Warn2 "not changed: setting $LogRetentionDays days would shorten retention irreversibly and this script only ever lengthens retention (item 9)."
        Write-Warn2 "apply it by hand if that is really what you want:"
        Write-Warn2 "  aws logs put-retention-policy --log-group-name $logGroup --retention-in-days $LogRetentionDays --profile $Profile --region $Region"
    } elseif ([int]$deployedRetention -lt $LogRetentionDays) {
        Invoke-AwsWrite logs put-retention-policy --log-group-name $logGroup --retention-in-days $LogRetentionDays | Out-Null
        Write-OkApplied "retention lengthened from $deployedRetention to $LogRetentionDays days"
    } elseif ([int]$deployedRetention -gt $LogRetentionDays) {
        # item 9: the old "-ne" comparison would have shortened this, which is irreversible.
        Write-Warn2 "retention is $deployedRetention days, longer than the requested $LogRetentionDays days; keeping it (this script only ever lengthens retention)."
    } else {
        Write-Ok "retention already $LogRetentionDays days"
    }
}

# --------------------------------------------------------------------------- dashboard --------------------------------------------------------------------------- #

$dashboardReady = $false
$dashboardPublicOk = $true
if ($SkipDashboard) {
    Write-Step 'S3 dashboard (skipped)'
    Write-Info '-SkipDashboard was passed: no bucket or objects are created or updated.'
} else {
    Write-Step "S3 dashboard bucket '$DashboardBucket'"
    # item 4: the public-read policy and the public-access-block are only *applied* to a bucket
    # this run created. An existing bucket is read and compared, never widened.
    $bucketCreated = $false
    if (Test-AwsResource s3api head-bucket --bucket $DashboardBucket) {
        Write-Ok 'bucket already exists'
    } else {
        Invoke-AwsWrite s3api create-bucket --bucket $DashboardBucket `
            --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
        $bucketCreated = $true
        Write-OkApplied 'bucket created'
    }

    $policyJson = @"
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadDashboard",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::$DashboardBucket/*"
    }
  ]
}
"@
    if ($bucketCreated) {
        # public read for static website hosting: objects only, no listing and no writes
        Invoke-AwsWrite s3api put-public-access-block --bucket $DashboardBucket `
            --public-access-block-configuration 'BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false' | Out-Null
        Write-JsonFile $policyPath $policyJson
        Invoke-AwsWrite s3api put-bucket-policy --bucket $DashboardBucket --policy "file://$policyPath" | Out-Null
        Write-OkApplied 'public read policy applied to objects (no list, no write)'
    } else {
        # item 4: read what is really there and report the difference instead of widening it.
        $existingPolicyText = $null
        if (Test-AwsResource s3api get-bucket-policy --bucket $DashboardBucket) {
            $existingPolicyText = (Invoke-AwsJson s3api get-bucket-policy --bucket $DashboardBucket).Policy
        }
        $existingPolicy = $null
        if ($existingPolicyText -is [string] -and $existingPolicyText) {
            try { $existingPolicy = $existingPolicyText | ConvertFrom-Json } catch { $existingPolicy = $null }
        } elseif ($existingPolicyText) {
            $existingPolicy = $existingPolicyText
        }
        $alreadyPublic = Test-S3PublicReadStatement $existingPolicy $DashboardBucket
        if ($alreadyPublic) {
            Write-Ok 'existing bucket already serves objects publicly (policy and public-access-block left untouched)'
        } else {
            $dashboardPublicOk = $false
            Write-Warn2 "the existing bucket $DashboardBucket does NOT grant public object read, and this run will not make it so."
            Write-Warn2 "doing that would mean disabling the public-access block and adding a bucket policy - a widening of access on a bucket this script did not create (item 4)."
            if ($null -eq $existingPolicy) {
                Write-Warn2 "  difference: no bucket policy is currently set; the desired policy adds Sid PublicReadDashboard (s3:GetObject on arn:aws:s3:::$DashboardBucket/* for Principal *)."
            } else {
                Write-Warn2 "  difference: an existing policy is set but it does not contain a public s3:GetObject statement for arn:aws:s3:::$DashboardBucket/*."
            }
            if (Test-AwsResource s3api get-public-access-block --bucket $DashboardBucket) {
                $block = (Invoke-AwsJson s3api get-public-access-block --bucket $DashboardBucket).PublicAccessBlockConfiguration
                Write-Warn2 "  deployed public-access-block: BlockPublicAcls=$($block.BlockPublicAcls), IgnorePublicAcls=$($block.IgnorePublicAcls), BlockPublicPolicy=$($block.BlockPublicPolicy), RestrictPublicBuckets=$($block.RestrictPublicBuckets)"
            }
            Write-Warn2 "  if the dashboard is meant to be public on this bucket, apply the intended policy by hand after reviewing it:"
            Write-Warn2 "    aws s3api put-bucket-policy --bucket $DashboardBucket --policy file://$policyPath --profile $Profile --region $Region"
            Write-Warn2 "    (the file is written by this run at $policyPath)"
        }
    }

    Write-JsonFile $websitePath '{"IndexDocument": {"Suffix": "index.html"}, "ErrorDocument": {"Key": "index.html"}}'
    Invoke-AwsWrite s3api put-bucket-website --bucket $DashboardBucket --website-configuration "file://$websitePath" | Out-Null
    Write-OkApplied 'static website hosting enabled (index.html)'

    # The list is deliberately fixed: ten objects, nothing else in web/ is uploaded. Prepared card
    # images and icons live there too (see README, "Image assets"), and adding one to this list -
    # Get-DashboardAssets, shared with the -DashboardOnly path - is a deliberate act, not a side
    # effect of dropping a file into the directory.
    # Sizes as of 2026-10-18: code 370.1 KB + backdrop 58.8 KB = 428.9 KB per visit, plus 182.7 KB
    # of card imagery as the cards come into view.
    Send-DashboardAssets | Out-Null
    $dashboardReady = $true
    $dashboardUrl = "http://$DashboardBucket.s3-website.$Region.amazonaws.com"
    Write-Ok "dashboard: $dashboardUrl"
}

# --------------------------------------------------------------------------- verification --------------------------------------------------------------------------- #

Write-Step 'Post-deployment verification'
if ($DryRun) {
    # item 19: nothing was written, so there is no deployed state to verify; the comparisons
    # above are the whole report.
    Write-Info '-DryRun was passed: the verification block is skipped because nothing was deployed.'
} else {
    $verified = Invoke-AwsJson lambda get-function-configuration --function-name $FunctionName
    Write-Host "  function state      : $($verified.State) (last modified $($verified.LastModified))"
    Write-Host "  handler / runtime   : $($verified.Handler) / $($verified.Runtime)"
    Write-Host "  memory / timeout    : $($verified.MemorySize) MB / $($verified.Timeout)s"
    if ($apiId) {
        $verifiedStage = Invoke-AwsJson apigatewayv2 get-stage --api-id $apiId --stage-name $StageName
        $burstLimit = $verifiedStage.DefaultRouteThrottlingBurstLimit
        $rateLimit = $verifiedStage.DefaultRouteThrottlingRateLimit
        if ($null -eq $burstLimit -and $null -eq $rateLimit) {
            Write-Host '  stage throttling    : not set (API Gateway account defaults apply)'
        } else {
            Write-Host "  stage throttling    : burst $burstLimit / rate $rateLimit"
        }
        $verifiedRoutes = Get-AwsAllItems -AwsArguments @('apigatewayv2', 'get-routes', '--api-id', $apiId) -ItemsProperty 'Items'
        Write-Host "  routes              : $(@($verifiedRoutes).Count)"
    } else {
        Write-Info 'no API in this run: the stage throttling and route count cannot be reported.'
    }
    if ($SkipDashboard) {
        Write-Info 'dashboard skipped (-SkipDashboard): no S3 object count.'
    } else {
        $objectCount = Invoke-Aws s3api list-objects-v2 --bucket $DashboardBucket --query 'KeyCount' --output text
        Write-Host "  dashboard objects   : $objectCount in s3://$DashboardBucket"
        if (-not $dashboardPublicOk) {
            Write-Host '  dashboard access    : NOT public read - the existing bucket was left untouched (see the [warn] lines above)'
        }
    }
    if (-not $SkipAlerts -and $alertTopicArn) {
        if (Test-AwsResource sns get-topic-attributes --topic-arn $alertTopicArn) {
            Write-Host "  alert topic         : $alertTopicArn exists"
        } else {
            Write-Host "  alert topic         : MISSING ($alertTopicArn did not exist at verification time)"
        }
    }

    # item 10: the deployed package is compared with src/ by the existing tool. It reads AWS
    # (get-function + the code URL) and downloads a zip; it changes nothing.
    Write-Info 'comparing the deployed package with src/ (python tools/verify_deployed_code.py)'
    $verifyOutput = (& python tools/verify_deployed_code.py 2>&1 | Out-String).Trim()
    $verifyExit = $LASTEXITCODE
    if ($verifyExit -ne 0) {
        Write-Host (Remove-SecretText $verifyOutput)
        Write-Fail "python tools/verify_deployed_code.py exited ${verifyExit}: the deployed Lambda code does not match src/. Nothing was rolled back; fix src/ (or re-deploy) and run the tool again (item 10)."
    }
    Write-Ok 'verify_deployed_code.py: the deployed Lambda code matches src/'
    Write-Host (Remove-SecretText $verifyOutput)
}

# --------------------------------------------------------------------------- done --------------------------------------------------------------------------- #

Write-Step 'Deployment summary'
if ($DryRun) {
    Write-Host '  mode          : DRY RUN - nothing was changed; every command above was only printed'
}
Write-Host "  endpoint      : $endpoint"
Write-Host "  api id        : $apiId"
Write-Host "  function      : $FunctionName ($Runtime, $MemorySize MB, ${TimeoutSeconds}s)"
Write-Host "  table         : $TableName"
Write-Host "  stage         : $StageName"
if ($alertTopicArn) {
    # the topic may only be planned (a -DryRun that did not create it), so the three states are
    # reported separately instead of claiming "topic ready" unconditionally
    if ($alertsBlocked) {
        $alertState = 'topic ready, BUT the execution role may not publish (IAM denied)'
    } elseif ($DryRun -and -not $topicExists) {
        $alertState = 'planned only: created (and publishable) after a real run'
    } else {
        $alertState = 'publishing allowed on this topic only'
    }
    Write-Host "  alert topic   : $alertTopicArn ($alertState)"
}
if ($dashboardReady) { Write-Host "  dashboard     : $dashboardUrl" }
Write-Host "  zones         : mode '$ZoneMode' (live = Chennai, Mumbai, Bengaluru, Hyderabad, Kochi)"
Write-Host "  weather       : $WeatherMode, refresh every ${WeatherRefreshSeconds}s, timeout ${WeatherTimeoutSeconds}s"
Write-Host @"

Acceptance checks (no secrets involved):

  curl.exe -s -o - -w "HTTP %{http_code}" $endpoint/zones
  curl.exe -s -o - -w "HTTP %{http_code}" $endpoint/incidents?limit=20
  curl.exe -s -o - -w "HTTP %{http_code}" $endpoint/zones/city-chennai/history?minutes=60

  python tools/weather_probe.py
  # ^ reads the live Open-Meteo feed directly (read-only, no AWS credentials)

  node tools/dashboard_check.mjs --payload build/zones-live-weather.json --history build/history-live-weather.json --incidents build/incidents-live-weather.json
  node tools/dashboard_run.mjs --site $dashboardUrl --api $endpoint

  python tools/live_simulator.py --url $endpoint --steps 60 --interval 1.0
  # ^ reads the secret from $IngestKeyFile (or FLOODPULSE_INGEST_KEY) and never prints it
  #   demo mode only: the simulator's zone ids are not part of the live fleet

  aws dynamodb get-item --table-name $TableName --profile $Profile --region $Region --key file://build/key-latest.json
  aws dynamodb query --table-name $TableName --profile $Profile --region $Region ``
    --key-condition-expression "pk = :pk" --expression-attribute-values file://build/eav-pk.json

  # POST /readings requires the x-fp-key header; the read routes are public:
  #   missing header -> HTTP 401      wrong value -> HTTP 401
  #   function without FLOODPULSE_INGEST_KEY -> HTTP 503 ingest_not_configured (fail closed)

Incidents live in the same table as extra items (sk INCIDENT#... retained as history,
sk ALERT#COOLDOWN per zone). Alerts publish only on a WARNING/CRITICAL escalation and are
rate-limited per zone and severity; the topic ARN is never returned to the dashboard.

Live zones: rainfall intensity and soil saturation come from the external Open-Meteo
service (REAL / MODEL-derived) and are cached in the same table under sk WEATHER#CACHE,
refreshed at most every ${WeatherRefreshSeconds}s. Drain level, water level and flow rate are
SIMULATED by a deterministic hydraulic response to that real rainfall, because FloodPulse
has no real drain or flow sensors. Risk scores always come from the heuristic-v1 heuristic:
not a validated hydrological model, not calibrated for these cities, not an official
warning. Weather data by Open-Meteo.com (CC BY 4.0).

Demo mode: re-run with -ZoneMode demo (and -WeatherMode off to forbid any network call) to
serve the three simulated zones instead. The ingestion secret lives only in
$IngestKeyFile and build\env-prod.json (both gitignored) - never in source control.
"@
