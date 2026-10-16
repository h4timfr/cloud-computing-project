<#
.SYNOPSIS
  Idempotent FloodPulse deployment: one DynamoDB table, one Lambda, one HTTP API,
  one S3 static dashboard.

.DESCRIPTION
  Creates or updates exactly these resources, and nothing else:
    * DynamoDB table (pk/sk, PAY_PER_REQUEST, TTL on "ttl")
    * Lambda function (python3.13, 256 MB, 10 s, handler floodpulse.handler.lambda_handler)
    * API Gateway HTTP API with POST /readings, GET /zones, GET /zones/{zone_id}/history + CORS
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

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -RoleName floodpulse-lambda-role
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -SkipDashboard

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
    [switch]$SkipAlerts
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
    #>
    param([string]$Path, [string]$Content)
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
        throw "aws $($AwsArguments -join ' ') failed (exit $LASTEXITCODE):`n$output"
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
    <# True when the AWS CLI command succeeds, false when it fails (e.g. not found). #>
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & aws @AwsArguments --profile $Profile --region $Region *> $null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $previous
    }
}

# --------------------------------------------------------------------------- preflight --------------------------------------------------------------------------- #

Write-Step "FloodPulse deploy (profile $Profile, region $Region)"
$identity = Invoke-AwsJson sts get-caller-identity
Write-Ok "identity: $($identity.Arn) (account $($identity.Account))"
if ($identity.Arn -match ':root$') { throw 'refusing to deploy with the AWS root account' }

if (-not (Test-AwsResource iam get-role --role-name $RoleName)) {
    throw "execution role '$RoleName' was not found. Create it first (trust lambda.amazonaws.com, logs:* plus dynamodb:PutItem/GetItem/Query on table/$TableName), then re-run this script."
}
$roleArn = (Invoke-AwsJson iam get-role --role-name $RoleName).Role.Arn
Write-Ok "execution role: $roleArn"

# --------------------------------------------------------------------------- build --------------------------------------------------------------------------- #

Write-Step 'Building deployment package'
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
$keyPath = Join-Path $buildDir 'key-latest.json'
$eavPath = Join-Path $buildDir 'eav-pk.json'
Write-JsonFile $keyPath '{"pk": {"S": "ZONE#zone-riverside-north"}, "sk": {"S": "LATEST"}}'
Write-JsonFile $eavPath '{":pk": {"S": "ZONE#zone-riverside-north"}}'

# --------------------------------------------------------------------------- secret + env --------------------------------------------------------------------------- #

Write-Step 'Ingestion secret and Lambda environment'
if (-not (Test-Path $envPath)) {
    if (-not (Test-Path $keyFilePath)) {
        Write-Info "no local secret yet; generating $IngestKeyFile and $envPath"
    }
    $initArgs = @('tools/init_ingest_key.py', '--key-file', $keyFilePath, '--lambda-env', $envPath, '--table', $TableName)
    if (Test-Path $keyFilePath) { $initArgs += '--reuse' }
    $initOutput = (& python @initArgs 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "failed to prepare the ingestion secret:`n$initOutput" }
    Write-Ok "secret prepared (value never displayed): $IngestKeyFile + $envPath"
} else {
    Write-Ok "environment file already present: $envPath (gitignored)"
}
if (-not (Test-Path $keyFilePath)) {
    throw "missing '$IngestKeyFile'. Create it with: python tools/init_ingest_key.py --lambda-env build\env-prod.json"
}
$envVariables = (Get-Content -Raw -Path $envPath | ConvertFrom-Json).Variables
$envNames = @($envVariables.PSObject.Properties.Name)
Write-Info "lambda environment variables: $($envNames -join ', ') (values not displayed)"
if ($envNames -notcontains 'FLOODPULSE_INGEST_KEY') {
    Write-Warn2 'the environment file has no FLOODPULSE_INGEST_KEY: cloud ingestion will fail closed with HTTP 503.'
    Write-Warn2 "regenerate it with: python tools/init_ingest_key.py --key-file $IngestKeyFile --lambda-env build\env-prod.json"
}

# This deploy applies the *environment file*. The key file is the documented source of truth,
# so a rotation that updated only the key file would be silently undone here. Report the
# disagreement (fingerprints only, never the values) and leave both files alone.
if ($envNames -contains 'FLOODPULSE_INGEST_KEY') {
    $envKey = (Get-Content -Raw -Path $envPath | ConvertFrom-Json).Variables.FLOODPULSE_INGEST_KEY
    $fileKey = (Get-Content -Raw -Path $keyFilePath).Trim()
    if ($envKey -ne $fileKey) {
        Write-Warn2 "the environment file and $IngestKeyFile carry different ingestion keys ($(Get-SecretFingerprint $envKey) vs $(Get-SecretFingerprint $fileKey))."
        Write-Warn2 'this deploy would apply the environment file key, which would undo a rotation made only in the key file.'
        Write-Warn2 "fix it with: python tools/init_ingest_key.py --reuse --key-file $IngestKeyFile --lambda-env build\env-prod.json"
    } else {
        Write-Ok "environment file and $IngestKeyFile agree on the ingestion key ($(Get-SecretFingerprint $fileKey))"
    }
}

# --------------------------------------------------------------------------- sns topic --------------------------------------------------------------------------- #

if (-not $SkipAlerts) {
    Write-Step "SNS alert topic '$AlertTopicName'"
    $alertTopicArn = (Invoke-AwsJson sns create-topic --name $AlertTopicName --attributes 'DisplayName=FloodPulse Alerts').TopicArn
    if (-not $alertTopicArn) { throw "could not resolve the ARN of SNS topic '$AlertTopicName'" }
    Write-Ok "topic ready: $alertTopicArn (created if it was missing; existing topics are never reused silently under another name)"

    $subscriptions = Invoke-AwsJson sns list-subscriptions-by-topic --topic-arn $alertTopicArn
    $confirmed = @($subscriptions.Subscriptions | Where-Object { $_.SubscriptionArn -ne 'PendingConfirmation' }).Count
    $pending = @($subscriptions.Subscriptions | Where-Object { $_.SubscriptionArn -eq 'PendingConfirmation' }).Count
    if ($confirmed -eq 0) {
        Write-Warn2 "no confirmed subscription on the topic: publishes will succeed but nobody is notified."
        Write-Warn2 "add an email subscriber with (the address is the operator's choice):"
        Write-Warn2 "  aws sns subscribe --topic-arn $alertTopicArn --protocol email --notification-endpoint <address> --profile $Profile --region $Region"
    } else {
        Write-Ok "$confirmed confirmed subscription(s), $pending pending"
    }

    # merge the topic ARN (and the cooldown) into the gitignored env file: only the
    # variable names are ever printed, never the ingestion secret
    $envDocument = Get-Content -Raw -Path $envPath | ConvertFrom-Json
    $envDocument.Variables | Add-Member -Force -NotePropertyName 'FLOODPULSE_SNS_TOPIC_ARN' -NotePropertyValue $alertTopicArn
    $envDocument.Variables | Add-Member -Force -NotePropertyName 'FLOODPULSE_ALERT_COOLDOWN_SECONDS' -NotePropertyValue "$AlertCooldownSeconds"
    Write-JsonFile $envPath ($envDocument | ConvertTo-Json -Depth 6)
    $envVariables = (Get-Content -Raw -Path $envPath | ConvertFrom-Json).Variables
    $envNames = @($envVariables.PSObject.Properties.Name)
    Write-Ok "environment file updated: $($envNames -join ', ') (values not displayed; cooldown ${AlertCooldownSeconds}s)"

    # the execution role must be allowed to publish to this one topic and nothing else.
    # This needs iam:PutRolePolicy; if that is denied we report it and carry on, because
    # incident tracking and the dashboard do not depend on alerting.
    $rolePolicies = Invoke-AwsJson iam list-role-policies --role-name $RoleName
    if (@($rolePolicies.PolicyNames) -contains 'FloodPulseSnsPublish') {
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
            Invoke-Aws iam put-role-policy --role-name $RoleName --policy-name FloodPulseSnsPublish --policy-document "file://$snsPolicyPath" | Out-Null
            Write-Ok "inline policy FloodPulseSnsPublish attached (sns:Publish on $alertTopicArn only)"
        } catch {
            $alertsBlocked = $true
            Write-Warn2 'IAM DENIED: could not attach FloodPulseSnsPublish; alerting will report publish failures.'
            Write-Warn2 ($_.Exception.Message -replace "`n", ' ')
            Write-Warn2 "operator action: attach this inline policy to $RoleName, then re-run this script:"
            Write-Warn2 "  aws iam put-role-policy --role-name $RoleName --policy-name FloodPulseSnsPublish --policy-document file://$snsPolicyPath --profile $Profile --region $Region"
        }
    }
    $envVariables = (Get-Content -Raw -Path $envPath | ConvertFrom-Json).Variables
    $envNames = @($envVariables.PSObject.Properties.Name)
}

# ------------------------------------------------------- M4: zones and live weather ------------------------------------------------------- #
# The zone mode decides which fleet is live: 'live' = the five Indian city zones fed by
# Open-Meteo, 'demo' = the three simulated zones. The weather switch is a kill switch:
# 'off' makes the Lambda serve cached/simulated data only and never calls the network.
# These are merged after the SNS step so they are always present, whatever is skipped.

Write-Step "Zone mode and external weather ($ZoneMode / $WeatherMode)"
$envDocument = Get-Content -Raw -Path $envPath | ConvertFrom-Json
$envDocument.Variables | Add-Member -Force -NotePropertyName 'FLOODPULSE_ZONE_MODE' -NotePropertyValue "$ZoneMode"
$envDocument.Variables | Add-Member -Force -NotePropertyName 'FLOODPULSE_WEATHER' -NotePropertyValue "$WeatherMode"
$envDocument.Variables | Add-Member -Force -NotePropertyName 'FLOODPULSE_WEATHER_REFRESH_SECONDS' -NotePropertyValue "$WeatherRefreshSeconds"
$envDocument.Variables | Add-Member -Force -NotePropertyName 'FLOODPULSE_WEATHER_TIMEOUT_SECONDS' -NotePropertyValue "$WeatherTimeoutSeconds"
Write-JsonFile $envPath ($envDocument | ConvertTo-Json -Depth 6)
$envVariables = (Get-Content -Raw -Path $envPath | ConvertFrom-Json).Variables
$envNames = @($envVariables.PSObject.Properties.Name)
Write-Ok "environment file updated: $($envNames -join ', ') (values not displayed)"
Write-Info "zone mode '$ZoneMode' + weather '$WeatherMode' at most every ${WeatherRefreshSeconds}s; no new AWS resources are created"

# --------------------------------------------------------------------------- cors --------------------------------------------------------------------------- #

$dashboardUrl = "http://$DashboardBucket.s3-website.$Region.amazonaws.com"
$allowedOrigins = @($dashboardUrl)
if ($LocalDevOrigin) { $allowedOrigins += $LocalDevOrigin }
$corsJson = @{
    AllowOrigins = $allowedOrigins
    AllowMethods = @('GET', 'POST', 'OPTIONS')
    AllowHeaders = @('content-type', 'x-fp-key')
    MaxAge       = 300
} | ConvertTo-Json -Depth 4
Write-JsonFile $corsPath $corsJson
Write-Ok "CORS restricted to: $($allowedOrigins -join ', ')"

$zipBuilder = @"
import pathlib, zipfile
root = pathlib.Path(r'$srcDir')
out = pathlib.Path(r'$zipPath')
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(root.rglob('*.py')):
        archive.write(str(path), path.relative_to(root).as_posix())
print(out.stat().st_size)
"@
$zipSize = ($zipBuilder | python -).Trim()
if ($LASTEXITCODE -ne 0) { throw 'failed to build the deployment zip' }
Write-Ok "package: $zipPath ($zipSize bytes)"

# --------------------------------------------------------------------------- table --------------------------------------------------------------------------- #

Write-Step "DynamoDB table '$TableName'"
if (Test-AwsResource dynamodb describe-table --table-name $TableName) {
    Write-Ok 'table already exists'
} else {
    Invoke-Aws dynamodb create-table --table-name $TableName `
        --attribute-definitions AttributeName=pk,AttributeType=S AttributeName=sk,AttributeType=S `
        --key-schema AttributeName=pk,KeyType=HASH AttributeName=sk,KeyType=RANGE `
        --billing-mode PAY_PER_REQUEST | Out-Null
    Invoke-Aws dynamodb wait table-exists --table-name $TableName | Out-Null
    Write-Ok 'table created'
}
$ttl = Invoke-AwsJson dynamodb describe-time-to-live --table-name $TableName
if ($ttl.TimeToLiveDescription.TimeToLiveStatus -ne 'ENABLED') {
    Invoke-Aws dynamodb update-time-to-live --table-name $TableName --time-to-live-specification Enabled=true,AttributeName=ttl | Out-Null
    Write-Ok 'TTL enabled on "ttl"'
} else {
    Write-Ok "TTL already enabled on '$($ttl.TimeToLiveDescription.AttributeName)'"
}

# --------------------------------------------------------------------------- lambda --------------------------------------------------------------------------- #

Write-Step "Lambda function '$FunctionName'"
$functionExists = Test-AwsResource lambda get-function --function-name $FunctionName
if ($functionExists) {
    Invoke-Aws lambda update-function-code --function-name $FunctionName --zip-file "fileb://$zipPath" | Out-Null
    Invoke-Aws lambda wait function-updated --function-name $FunctionName | Out-Null
    Write-Ok 'code updated from the local package'
    $config = (Invoke-AwsJson lambda get-function-configuration --function-name $FunctionName)
    if ($config.MemorySize -ne $MemorySize -or $config.Timeout -ne $TimeoutSeconds -or $config.Runtime -ne $Runtime) {
        Invoke-Aws lambda update-function-configuration --function-name $FunctionName `
            --runtime $Runtime --memory-size $MemorySize --timeout $TimeoutSeconds `
            --handler floodpulse.handler.lambda_handler | Out-Null
        Invoke-Aws lambda wait function-updated --function-name $FunctionName | Out-Null
        Write-Ok "configuration corrected (memory $MemorySize MB, timeout ${TimeoutSeconds}s, $Runtime)"
    } else {
        Write-Ok "configuration already correct (memory $MemorySize MB, timeout ${TimeoutSeconds}s, $Runtime)"
    }
    # the environment is always re-applied from the gitignored file so the ingestion
    # secret cannot silently drift; only the variable names are ever displayed
    Invoke-Aws lambda update-function-configuration --function-name $FunctionName `
        --environment "file://$envPath" | Out-Null
    Invoke-Aws lambda wait function-updated --function-name $FunctionName | Out-Null
    $appliedNames = @(((Invoke-AwsJson lambda get-function-configuration --function-name $FunctionName).Environment.Variables).PSObject.Properties.Name)
    Write-Ok "environment applied: $($appliedNames -join ', ') (values not displayed)"
} else {
    Invoke-Aws lambda create-function --function-name $FunctionName `
        --runtime $Runtime --role $roleArn --handler floodpulse.handler.lambda_handler `
        --memory-size $MemorySize --timeout $TimeoutSeconds `
        --zip-file "fileb://$zipPath" --environment "file://$envPath" | Out-Null
    Invoke-Aws lambda wait function-active --function-name $FunctionName | Out-Null
    Write-Ok 'function created'
}
$functionArn = (Invoke-AwsJson lambda get-function-configuration --function-name $FunctionName).FunctionArn

# --------------------------------------------------------------------------- http api --------------------------------------------------------------------------- #

Write-Step "HTTP API '$ApiName'"
$apis = Invoke-AwsJson apigatewayv2 get-apis
$api = $null
if ($apis -and $apis.Items) { $api = $apis.Items | Where-Object { $_.Name -eq $ApiName } | Select-Object -First 1 }
if ($null -eq $api) {
    $api = Invoke-AwsJson apigatewayv2 create-api --name $ApiName --protocol-type HTTP --cors-configuration "file://$corsPath"
    Write-Ok "API created: $($api.ApiId)"
} else {
    Invoke-Aws apigatewayv2 update-api --api-id $api.ApiId --cors-configuration "file://$corsPath" | Out-Null
    Write-Ok "API already exists: $($api.ApiId) (CORS refreshed)"
}
$apiId = $api.ApiId
$endpoint = $api.ApiEndpoint

Write-Step 'API -> Lambda invoke permission'
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
    Invoke-Aws lambda add-permission --function-name $FunctionName --statement-id $statementId `
        --action lambda:InvokeFunction --principal apigateway.amazonaws.com --source-arn $sourceArn | Out-Null
    Write-Ok "granted apigateway.amazonaws.com invoke on $sourceArn"
}

Write-Step 'Routes and integration'
$integrations = Invoke-AwsJson apigatewayv2 get-integrations --api-id $apiId
$integration = $null
if ($integrations -and $integrations.Items) { $integration = $integrations.Items | Select-Object -First 1 }
if ($null -eq $integration) {
    $integration = Invoke-AwsJson apigatewayv2 create-integration --api-id $apiId --integration-type AWS_PROXY `
        --integration-uri $functionArn --payload-format-version 2.0
    Write-Ok "integration created: $($integration.IntegrationId)"
} else {
    Write-Ok "integration already exists: $($integration.IntegrationId)"
}
$target = "integrations/$($integration.IntegrationId)"
$routes = Invoke-AwsJson apigatewayv2 get-routes --api-id $apiId
foreach ($routeKey in @('POST /readings', 'GET /zones', 'GET /zones/{zone_id}/history', 'GET /incidents')) {
    $existing = $null
    if ($routes -and $routes.Items) { $existing = $routes.Items | Where-Object { $_.RouteKey -eq $routeKey } | Select-Object -First 1 }
    if ($null -eq $existing) {
        Invoke-Aws apigatewayv2 create-route --api-id $apiId --route-key $routeKey --target $target | Out-Null
        Write-Ok "route created: $routeKey"
    } elseif ($existing.Target -ne $target) {
        Invoke-Aws apigatewayv2 update-route --api-id $apiId --route-id $existing.RouteId --target $target | Out-Null
        Write-Ok "route retargeted: $routeKey"
    } else {
        Write-Ok "route exists: $routeKey"
    }
}

Write-Step "Stage '$StageName'"
$stages = Invoke-AwsJson apigatewayv2 get-stages --api-id $apiId
$stage = $null
if ($stages -and $stages.Items) { $stage = $stages.Items | Where-Object { $_.StageName -eq $StageName } | Select-Object -First 1 }
if ($null -eq $stage) {
    Invoke-Aws apigatewayv2 create-stage --api-id $apiId --stage-name $StageName --auto-deploy | Out-Null
    Write-Ok 'stage created with auto-deploy'
} else {
    Write-Ok 'stage already exists'
}

# --------------------------------------------------------------------------- logs --------------------------------------------------------------------------- #

Write-Step 'CloudWatch log retention'
$logGroup = "/aws/lambda/$FunctionName"
$groups = Invoke-AwsJson logs describe-log-groups --log-group-name-prefix $logGroup
$group = $null
if ($groups -and $groups.logGroups) { $group = $groups.logGroups | Where-Object { $_.logGroupName -eq $logGroup } | Select-Object -First 1 }
if ($null -eq $group) {
    Write-Info "log group not created yet (it appears on the first invocation); set retention afterwards with:"
    Write-Info "  aws logs put-retention-policy --log-group-name $logGroup --retention-in-days $LogRetentionDays --profile $Profile --region $Region"
} elseif ($group.retentionInDays -ne $LogRetentionDays) {
    Invoke-Aws logs put-retention-policy --log-group-name $logGroup --retention-in-days $LogRetentionDays | Out-Null
    Write-Ok "retention set to $LogRetentionDays days"
} else {
    Write-Ok "retention already $LogRetentionDays days"
}

# --------------------------------------------------------------------------- dashboard --------------------------------------------------------------------------- #

$dashboardReady = $false
if ($SkipDashboard) {
    Write-Step 'S3 dashboard (skipped)'
    Write-Info '-SkipDashboard was passed: no bucket or objects are created or updated.'
} else {
    Write-Step "S3 dashboard bucket '$DashboardBucket'"
    if (Test-AwsResource s3api head-bucket --bucket $DashboardBucket) {
        Write-Ok 'bucket already exists'
    } else {
        Invoke-Aws s3api create-bucket --bucket $DashboardBucket `
            --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
        Write-Ok 'bucket created'
    }

    # public read for static website hosting: objects only, no listing and no writes
    Invoke-Aws s3api put-public-access-block --bucket $DashboardBucket `
        --public-access-block-configuration 'BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false' | Out-Null
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
    Write-JsonFile $policyPath $policyJson
    Invoke-Aws s3api put-bucket-policy --bucket $DashboardBucket --policy "file://$policyPath" | Out-Null
    Write-Ok 'public read policy applied to objects (no list, no write)'

    Write-JsonFile $websitePath '{"IndexDocument": {"Suffix": "index.html"}, "ErrorDocument": {"Key": "index.html"}}'
    Invoke-Aws s3api put-bucket-website --bucket $DashboardBucket --website-configuration "file://$websitePath" | Out-Null
    Write-Ok 'static website hosting enabled (index.html)'

    # uploads are small (~72 KB in total); long-lived caching only for the background image
    $assets = @(
        @{ File = 'index.html';    Type = 'text/html; charset=utf-8';              Cache = 'no-cache' },
        @{ File = 'styles.css';    Type = 'text/css; charset=utf-8';               Cache = 'no-cache' },
        @{ File = 'lib.js';        Type = 'application/javascript; charset=utf-8'; Cache = 'no-cache' },
        @{ File = 'app.js';        Type = 'application/javascript; charset=utf-8'; Cache = 'no-cache' },
        @{ File = 'bg-storm.webp'; Type = 'image/webp';                            Cache = 'public, max-age=86400' }
    )
    foreach ($asset in $assets) {
        $source = Join-Path $webDir $asset.File
        if (-not (Test-Path $source)) { throw "dashboard asset missing: $source" }
        Invoke-Aws s3 cp $source "s3://$DashboardBucket/$($asset.File)" `
            --content-type $asset.Type --cache-control $asset.Cache | Out-Null
        Write-Ok "uploaded $($asset.File) ($($asset.Type))"
    }
    $dashboardReady = $true
    $dashboardUrl = "http://$DashboardBucket.s3-website.$Region.amazonaws.com"
    Write-Ok "dashboard: $dashboardUrl"
}

# --------------------------------------------------------------------------- done --------------------------------------------------------------------------- #

Write-Step 'Deployment summary'
Write-Host "  endpoint      : $endpoint"
Write-Host "  api id        : $apiId"
Write-Host "  function      : $FunctionName ($Runtime, $MemorySize MB, ${TimeoutSeconds}s)"
Write-Host "  table         : $TableName"
Write-Host "  stage         : $StageName"
if ($alertTopicArn) {
    $alertState = if ($alertsBlocked) { 'topic ready, BUT the execution role may not publish (IAM denied)' } else { 'publishing allowed on this topic only' }
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
