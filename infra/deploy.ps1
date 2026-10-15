<#
.SYNOPSIS
  Idempotent FloodPulse M1 deployment: one DynamoDB table, one Lambda, one HTTP API.

.DESCRIPTION
  Creates or updates exactly these resources, and nothing else:
    * DynamoDB table (pk/sk, PAY_PER_REQUEST, TTL on "ttl")
    * Lambda function (python3.13, 256 MB, 10 s, handler floodpulse.handler.lambda_handler)
    * API Gateway HTTP API with POST /readings, GET /zones, GET /zones/{zone_id}/history + CORS
    * the API -> Lambda invoke permission, scoped to that API
    * CloudWatch log retention for the function's log group

  Properties of this script:
    * additive only: it never deletes, replaces or force-updates a resource
    * idempotent: safe to re-run; existing resources are detected and left alone
    * IAM is read-only here: the execution role is managed by the project owner and is
      only looked up (create it as "floodpulse-lambda-role" with lambda.amazonaws.com
      trust plus logs:* and dynamodb:PutItem/GetItem/Query on the FloodPulse table)
    * no credentials are created, printed or stored

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1
  powershell -ExecutionPolicy Bypass -File infra\deploy.ps1 -RoleName floodpulse-lambda-role

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
    [int]$TimeoutSeconds = 10,
    [int]$LogRetentionDays = 7
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$buildDir = Join-Path $repoRoot 'build'
$zipPath = Join-Path $buildDir 'floodpulse-api.zip'
$envPath = Join-Path $buildDir 'env.json'
$corsPath = Join-Path $buildDir 'cors.json'
$srcDir = Join-Path $repoRoot 'src'

function Write-Step { param([string]$Message) Write-Host "`n=== $Message ===" -ForegroundColor Cyan }
function Write-Ok { param([string]$Message) Write-Host "  [ok]   $Message" -ForegroundColor Green }
function Write-Info { param([string]$Message) Write-Host "  [info] $Message" }
function Write-Warn2 { param([string]$Message) Write-Host "  [warn] $Message" -ForegroundColor Yellow }

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

Write-Step "FloodPulse M1 deploy (profile $Profile, region $Region)"
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
if (-not (Test-Path $envPath)) {
    @"
{
  "Variables": {
    "FLOODPULSE_STORE": "dynamodb",
    "FLOODPULSE_TABLE": "$TableName"
  }
}
"@ | Set-Content -Path $envPath -Encoding UTF8
}
if (-not (Test-Path $corsPath)) {
    @'
{
  "AllowOrigins": ["*"],
  "AllowMethods": ["GET", "POST", "OPTIONS"],
  "AllowHeaders": ["content-type", "x-fp-key"],
  "MaxAge": 300
}
'@ | Set-Content -Path $corsPath -Encoding UTF8
}
@"
{"pk": {"S": "ZONE#zone-riverside-north"}, "sk": {"S": "LATEST"}}
"@ | Set-Content -Path $keyPath -Encoding UTF8
@'
{":pk": {"S": "ZONE#zone-riverside-north"}}
'@ | Set-Content -Path $eavPath -Encoding UTF8

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
            --handler floodpulse.handler.lambda_handler --environment "file://$envPath" | Out-Null
        Invoke-Aws lambda wait function-updated --function-name $FunctionName | Out-Null
        Write-Ok "configuration corrected (memory $MemorySize MB, timeout ${TimeoutSeconds}s, $Runtime)"
    } else {
        Write-Ok "configuration already correct (memory $MemorySize MB, timeout ${TimeoutSeconds}s, $Runtime)"
    }
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
foreach ($routeKey in @('POST /readings', 'GET /zones', 'GET /zones/{zone_id}/history')) {
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

# --------------------------------------------------------------------------- done --------------------------------------------------------------------------- #

Write-Step 'Deployment summary'
Write-Host "  endpoint      : $endpoint"
Write-Host "  api id        : $apiId"
Write-Host "  function      : $FunctionName ($Runtime, $MemorySize MB, ${TimeoutSeconds}s)"
Write-Host "  table         : $TableName"
Write-Host "  stage         : $StageName"
Write-Host @"

Acceptance checks (no secrets involved):

  curl.exe -s -o - -w "HTTP %{http_code}" $endpoint/zones
  curl.exe -s -o - -w "HTTP %{http_code}" $endpoint/zones/zone-riverside-north/history?minutes=60
  python tools/local_demo.py --steps 1        # prints a simulated payload you can POST

  aws dynamodb get-item --table-name $TableName --profile $Profile --region $Region --key file://build/key-latest.json
  aws dynamodb query --table-name $TableName --profile $Profile --region $Region ``
    --key-condition-expression "pk = :pk" --expression-attribute-values file://build/eav-pk.json

All FloodPulse sensor data is SIMULATED; risk scores are the heuristic-v1 heuristic,
not a validated prediction model.
"@
