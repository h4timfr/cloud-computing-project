# Offline verifier for the deployment script's safety logic.
#
# infra/deploy.ps1 is the only file that can change the live stack, and the one file the test
# suite cannot simply execute: a real run needs credentials and an account. So this script lifts
# the pure helpers out of it with the PowerShell parser, replaces every AWS entry point with a
# stub, and exercises the rules that make the dangerous operations safe - secret redaction, the
# not-found classifier, environment-document validation, pagination, and the -DryRun write gate.
#
# It never calls AWS and never reads the real secret. It is run by tests/test_deploy_script.py
# and can be run by hand:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\deploy_logic_check.ps1
$repoRoot = Split-Path -Parent $PSScriptRoot
$probeDir = Join-Path $repoRoot 'build\deploy-logic-probe'
$null = New-Item -ItemType Directory -Force -Path $probeDir
$target = Join-Path $repoRoot 'infra\deploy.ps1'
$source = Get-Content -Raw -Path $target

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($target, [ref]$tokens, [ref]$errors)
if ($errors -and $errors.Count -gt 0) { throw 'deploy.ps1 does not parse' }

$script:failures = 0
function Assert-True { param([bool]$Condition, [string]$Label) if ($Condition) { Write-Host "  [pass] $Label" } else { Write-Host "  [FAIL] $Label" -ForegroundColor Red; $script:failures++ } }
function Assert-False { param([bool]$Condition, [string]$Label) Assert-True (-not $Condition) $Label }
function Assert-Throws {
    param([scriptblock]$Action, [string]$Label, [string]$ExpectText = '')
    $threw = $false
    $message = ''
    try { & $Action | Out-Null } catch { $threw = $true; $message = $_.Exception.Message }
    Assert-True ($threw -and ($ExpectText -eq '' -or $message -like "*$ExpectText*")) "$Label (threw: $threw; message: $message)"
}

$script:KnownSecrets = New-Object System.Collections.ArrayList
$DryRun = $false
$Profile = 'cloud-project'
$Region = 'eu-north-1'
$envPath = Join-Path $probeDir 'probe-env.json'
$envDocument = $null

# --- stub the AWS entry points so nothing can reach the network -----------------------------
$script:awsJsonCalls = New-Object System.Collections.ArrayList
$script:awsWriteCalls = New-Object System.Collections.ArrayList
function Invoke-Aws { param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments) [void]$script:awsWriteCalls.Add(($AwsArguments -join ' ')); return 'aws-called' }
function Invoke-AwsJson { param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments) [void]$script:awsJsonCalls.Add(($AwsArguments -join ' ')); return $script:NextPage }

$wanted = @('Write-Fail', 'Write-OkApplied', 'Write-DryRun', 'Register-Secret', 'Remove-SecretText',
    'Compare-StringSet', 'Get-ValidatedEnvDocument', 'Test-S3PublicReadStatement', 'Get-SecretFingerprint',
    'Get-AwsAllItems', 'Invoke-AwsWrite', 'Wait-AwsState')
$defs = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
foreach ($def in $defs) {
    if ($wanted -contains $def.Name) { Invoke-Expression $def.Extent.Text }
}

Write-Host '== item 16: secret redaction =='
Register-Secret 'supersecretvalue123'
$redacted = Remove-SecretText 'boom: {"Variables":{"FLOODPULSE_INGEST_KEY":"supersecretvalue123","FLOODPULSE_ZONE_MODE":"live"}}'
Assert-False $redacted.Contains('supersecretvalue123') 'registered secret value is removed'
Assert-True $redacted.Contains('<redacted>') 'replacement marker present'
Assert-True $redacted.Contains('FLOODPULSE_ZONE_MODE') 'non-secret neighbours survive'
Assert-False (Remove-SecretText 'FLOODPULSE_INGEST_KEY=neverreadbythisprocess').Contains('neverreadbythisprocess') 'JSON/KEY=VALUE shape masked without reading the value'
Assert-False (Remove-SecretText '"FLOODPULSE_INGEST_KEY": "anothervalue"').Contains('anothervalue') 'quoted JSON pair masked'
Assert-True ((Remove-SecretText '') -eq '') 'null/empty text is safe'

Write-Host '== item 13 helper: set comparison =='
$diff = @(Compare-StringSet @('http://a', 'http://B') @('http://A'))
Assert-True ($diff.Count -eq 1 -and $diff[0] -eq 'http://B') 'case-insensitive comparison finds only the real difference'
Assert-True (@(Compare-StringSet @('x') @('X', 'y')).Count -eq 0) 'identical sets compare equal'

Write-Host '== item 4 helper: public-read detection =='
$publicPolicy = (@'
{"Version":"2012-10-17","Statement":[{"Sid":"PublicReadDashboard","Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::demo-bucket/*"}]}
'@ | ConvertFrom-Json)
Assert-True (Test-S3PublicReadStatement $publicPolicy 'demo-bucket') 'public GetObject statement detected'
$awsStarPolicy = (@'
{"Statement":[{"Effect":"Allow","Principal":{"AWS":"*"},"Action":["s3:GetObject"],"Resource":["arn:aws:s3:::demo-bucket/*"]}]}
'@ | ConvertFrom-Json)
Assert-True (Test-S3PublicReadStatement $awsStarPolicy 'demo-bucket') 'Principal {"AWS":"*"} detected'
$privatePolicy = (@'
{"Statement":[{"Effect":"Allow","Principal":{"AWS":"arn:aws:iam::123456789012:root"},"Action":"s3:GetObject","Resource":"arn:aws:s3:::demo-bucket/*"}]}
'@ | ConvertFrom-Json)
Assert-False (Test-S3PublicReadStatement $privatePolicy 'demo-bucket') 'non-public policy is not mistaken for public'
$otherBucket = (@'
{"Statement":[{"Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::some-other-bucket/*"}]}
'@ | ConvertFrom-Json)
Assert-False (Test-S3PublicReadStatement $otherBucket 'demo-bucket') 'another bucket does not count'
Assert-False (Test-S3PublicReadStatement $null 'demo-bucket') 'missing policy is not public'

Write-Host '== item 8: environment document shape validation =='
$good = Join-Path $probeDir 'probe-env-good.json'
$badNumber = Join-Path $probeDir 'probe-env-number.json'
$badArray = Join-Path $probeDir 'probe-env-array.json'
$badVariables = Join-Path $probeDir 'probe-env-badvars.json'
$badJson = Join-Path $probeDir 'probe-env-badjson.json'
$noVariables = Join-Path $probeDir 'probe-env-novars.json'
[System.IO.File]::WriteAllText($good, '{"Variables":{"FLOODPULSE_INGEST_KEY":"k","FLOODPULSE_ALERT_COOLDOWN_SECONDS":"600"}}')
[System.IO.File]::WriteAllText($badNumber, '{"Variables":{"FLOODPULSE_INGEST_KEY":"k","FLOODPULSE_ALERT_COOLDOWN_SECONDS":600}}')
[System.IO.File]::WriteAllText($badArray, '[1,2,3]')
[System.IO.File]::WriteAllText($badVariables, '{"Variables":["A"]}')
[System.IO.File]::WriteAllText($badJson, '{"Variables":')
[System.IO.File]::WriteAllText($noVariables, '{"NotVariables":{}}')
$doc = Get-ValidatedEnvDocument $good
Assert-True (@($doc.Variables.PSObject.Properties.Name).Count -eq 2) 'valid document is returned with both names'
Assert-Throws { Get-ValidatedEnvDocument $badNumber } 'a non-string value is rejected' 'FLOODPULSE_ALERT_COOLDOWN_SECONDS'
Assert-Throws { Get-ValidatedEnvDocument $badArray } 'a JSON array is rejected' 'JSON object'
Assert-Throws { Get-ValidatedEnvDocument $badVariables } 'a non-object Variables is rejected' "'Variables'"
Assert-Throws { Get-ValidatedEnvDocument $badJson } 'invalid JSON is rejected' 'not valid JSON'
Assert-Throws { Get-ValidatedEnvDocument $noVariables } 'a missing Variables member is rejected' 'Variables'
Assert-Throws { Get-ValidatedEnvDocument $badNumber } 'a rejected document never reaches AWS (the call throws instead)'

Write-Host '== item 7: not-found classifier =='
$patternMatch = [regex]::Match($source, "if \(\`$message -match '([^']+)'\)")
Assert-True $patternMatch.Success 'the classifier pattern was found in deploy.ps1'
$pattern = $patternMatch.Groups[1].Value
Write-Host "  pattern: $pattern"
$missingSamples = @(
    'An error occurred (ResourceNotFoundException) when calling the GetFunction operation: Function not found: arn:aws:lambda:eu-north-1:1:function:floodpulse-api',
    'An error occurred (NoSuchEntity) when calling the GetRole operation: The role with name floodpulse-lambda-role cannot be found.',
    'An error occurred (404) when calling the HeadBucket operation: Not Found',
    'An error occurred (NoSuchBucketPolicy) when calling the GetBucketPolicy operation',
    'An error occurred (NoSuchPublicAccessBlockConfiguration) when calling the GetPublicAccessBlock operation: The public access block was not found for the specified bucket'
)
foreach ($sample in $missingSamples) { Assert-True ($sample -match $pattern) "treated as MISSING: $($sample.Substring(0, [Math]::Min(60, $sample.Length)))" }
$errorSamples = @(
    'An error occurred (AccessDenied) when calling the DescribeTable operation: User: arn:aws:iam::1:user/x is not authorized to perform: dynamodb:DescribeTable',
    'An error occurred (ThrottlingException) when calling the GetApis operation: Rate exceeded',
    'An error occurred (ExpiredToken) when calling the GetCallerIdentity operation: The security token included in the request is expired',
    'An error occurred (InvalidClientTokenId) when calling the GetRole operation: The security token included in the request is invalid',
    'An error occurred (AccessDenied) when calling the ListTopics operation: User is not authorized to perform: SNS:ListTopics',
    'Could not connect to the endpoint URL: "https://lambda.eu-north-1.amazonaws.com/"'
)
foreach ($sample in $errorSamples) { Assert-False ($sample -match $pattern) "treated as ERROR (hard fail): $($sample.Substring(0, [Math]::Min(60, $sample.Length)))" }

Write-Host '== item 14: pagination helper =='
# the stub walks a real page sequence (a stub that always returned the same page would spin,
# and it is exactly what the token-repeat guard is there to catch)
$script:pageIndex = 0
$script:pages = @(
    @{ Items = @('first', 'second'); NextToken = 'TOKEN-1' },
    @{ Items = @('third'); NextToken = $null }
)
function Invoke-AwsJson {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$AwsArguments)
    [void]$script:awsJsonCalls.Add(($AwsArguments -join ' '))
    $page = $null
    if ($script:pageIndex -lt $script:pages.Count) { $page = $script:pages[$script:pageIndex] }
    $script:pageIndex++
    return $page
}
$items = Get-AwsAllItems -AwsArguments @('apigatewayv2', 'get-apis') -ItemsProperty 'Items'
Assert-True ($items.Count -eq 3) "both pages collected (got $($items.Count) items: $($items -join ','))"
Assert-True ($items -contains 'third') 'the item from page 2 is present'
Assert-True (@($script:awsJsonCalls | Where-Object { $_ -like '*--next-token TOKEN-1*' }).Count -eq 1) 'the second call carried the token as --next-token'
Assert-True ($script:awsJsonCalls[0] -notlike '*--next-token*') 'no token is sent on the first page'

$script:awsJsonCalls.Clear()
$script:pageIndex = 0
$script:pages = @(
    @{ PolicyNames = @('FloodPulseSnsPublish'); Marker = 'M1' },
    @{ PolicyNames = @('Other'); Marker = $null }
)
$null = Get-AwsAllItems -AwsArguments @('iam', 'list-role-policies', '--role-name', 'r') -ItemsProperty 'PolicyNames' -TokenArgument '--marker' -TokenProperty 'Marker'
Assert-True (@($script:awsJsonCalls | Where-Object { $_ -like '*--marker M1*' }).Count -eq 1) 'the IAM Marker shape uses --marker'

$script:pageIndex = 0
$script:pages = @(@{ Items = @('x'); NextToken = 'STUCK' }, @{ Items = @('x'); NextToken = 'STUCK' }, @{ Items = @('x'); NextToken = 'STUCK' })
Assert-Throws { Get-AwsAllItems -AwsArguments @('apigatewayv2', 'get-apis') -ItemsProperty 'Items' } 'a token that never advances fails instead of looping forever' 'did not advance'

$script:pageIndex = 0
$script:pages = @(@{ Items = $null; NextToken = $null })
$empty = Get-AwsAllItems -AwsArguments @('apigatewayv2', 'get-apis') -ItemsProperty 'Items'
Assert-True ($empty.Count -eq 0) 'an empty page yields no items (not $null, which would read the same but for the wrong reason)'

# A one-item account is the usual case here (one API, one integration, one log group), and a
# single item used to be unrolled into a bare object on the way out of the helper - where
# ".Count" is $null, which reads as "nothing there" and sends the caller down the create path.
# That is how a deploy would have created a *second* integration on a live API.
$script:pageIndex = 0
$script:pages = @(@{ Items = @(@{ IntegrationId = 'only-one' }); NextToken = $null })
$single = Get-AwsAllItems -AwsArguments @('apigatewayv2', 'get-integrations') -ItemsProperty 'Items'
Assert-True ($single.Count -eq 1) "a single item still answers .Count (got: $($single.Count))"
Assert-True ($single[0].IntegrationId -eq 'only-one') 'and the item that came back is the one that was there'
Assert-True ($single -is [System.Collections.IList]) 'the result is still a collection, not a bare object'
# and the call sites must not re-wrap it: @(command) would collect the array as one element
Assert-False ($source -match '@\(Get-AwsAllItems') 'no call site wraps the list helper in @(), which would make .Count always 1'
Assert-True ($source -match '\$integrations = Get-AwsAllItems') 'the integration list is read through the helper'

# the same shape through the IAM helper (a role with exactly one inline policy)
$script:pageIndex = 0
$script:pages = @(@{ PolicyNames = @('FloodPulseSnsPublish'); Marker = $null })
$onePolicy = Get-AwsAllItems -AwsArguments @('iam', 'list-role-policies', '--role-name', 'r') -ItemsProperty 'PolicyNames' -TokenArgument '--marker' -TokenProperty 'Marker'
Assert-True ($onePolicy.Count -eq 1) 'a single inline policy is reported as one policy'
Assert-True ($onePolicy -contains 'FloodPulseSnsPublish') 'and it is the expected one'

Write-Host '== item 19: the dry-run gate =='
$DryRun = $true
$written = Invoke-AwsWrite lambda update-function-configuration --function-name demo --environment file://build/env-merged.json
Assert-True ($null -eq $written) 'Invoke-AwsWrite returns nothing in a dry run'
Assert-True ($script:awsWriteCalls.Count -eq 0) 'no AWS call was made for a write path in a dry run'
Wait-AwsState lambda wait function-updated --function-name demo
Assert-True ($script:awsWriteCalls.Count -eq 0) 'waiters are skipped in a dry run'
$DryRun = $false
Write-Host ''
if ($script:failures -eq 0) {
    Write-Host 'LOGIC CHECK: all assertions passed'
    exit 0
}
Write-Host "LOGIC CHECK: $($script:failures) assertion(s) FAILED" -ForegroundColor Red
exit 1
