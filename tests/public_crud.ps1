param(
    [Parameter(Mandatory = $true)][string]$AccessKey,
    [string]$BaseUrl = 'https://cloud.example.com',
    [string]$HostName = 'cloud.example.com'
)

$ErrorActionPreference = 'Stop'
$id = [guid]::NewGuid().ToString('N').Substring(0, 12)
$origin = ([uri]$BaseUrl).GetLeftPart([System.UriPartial]::Authority)
$headersFile = Join-Path $env:TEMP "rkcloud-login-$id.headers"
$bodyFile = Join-Path $env:TEMP "rkcloud-body-$id.json"

function Invoke-CloudCurl {
    param([string[]]$Arguments)
    $output = & curl.exe --noproxy '*' -sS @Arguments
    if ($LASTEXITCODE -ne 0) { throw "curl failed with exit code $LASTEXITCODE" }
    return $output
}

try {
    Invoke-CloudCurl @(
        '-D', $headersFile, '-o', 'NUL', '-X', 'POST',
        '-H', "Host: $HostName", '-H', "Origin: $origin",
        '--data-urlencode', "key=$AccessKey", '--data-urlencode', 'next=/',
        "$BaseUrl/login"
    ) | Out-Null
    $cookieLine = Get-Content -LiteralPath $headersFile |
        Where-Object { $_ -like 'Set-Cookie:*' } | Select-Object -First 1
    if (-not $cookieLine) { throw 'login did not return a session cookie' }
    $cookie = (($cookieLine -replace '^Set-Cookie:\s*', '').Split(';')[0])

    foreach ($disk in @('SSD', 'USB')) {
        $source = [uri]::EscapeDataString($disk)
        $original = "/rk-cloud-e2e-$id.txt"
        $renamed = "/rk-cloud-e2e-$id-renamed.txt"
        $payload = "RK Cloud end-to-end test $id on $disk"
        try {
            $status = Invoke-CloudCurl @(
                '-o', 'NUL', '-w', '%{http_code}', '-X', 'POST',
                '-H', "Host: $HostName", '-H', "Cookie: $cookie",
                '--data-binary', $payload,
                "$BaseUrl/api/resources?source=$source&path=$original&override=false"
            )
            if ($status -ne '200') { throw "$disk upload returned HTTP $status" }

            Invoke-CloudCurl @(
                '-o', $bodyFile, '-H', "Host: $HostName", '-H', "Cookie: $cookie",
                "$BaseUrl/api/resources?source=$source&path=$original&content=true"
            ) | Out-Null
            $resource = Get-Content -Raw -LiteralPath $bodyFile | ConvertFrom-Json
            if ($resource.content -ne $payload) { throw "$disk uploaded content did not match" }

            $move = @{
                items = @(@{
                    fromSource = $disk; fromPath = $original
                    toSource = $disk; toPath = $renamed
                })
                action = 'rename'; overwrite = $false; rename = $false
            } | ConvertTo-Json -Depth 5 -Compress
            $status = Invoke-CloudCurl @(
                '-o', $bodyFile, '-w', '%{http_code}', '-X', 'PATCH',
                '-H', "Host: $HostName", '-H', "Cookie: $cookie",
                '-H', 'Content-Type: application/json', '--data-binary', $move,
                "$BaseUrl/api/resources"
            )
            if ($status -ne '200') { throw "$disk rename returned HTTP $status" }

            $status = Invoke-CloudCurl @(
                '-o', 'NUL', '-w', '%{http_code}', '-X', 'DELETE',
                '-H', "Host: $HostName", '-H', "Cookie: $cookie",
                "$BaseUrl/api/resources?source=$source&path=$renamed"
            )
            if ($status -ne '200') { throw "$disk delete returned HTTP $status" }
            Write-Output "$disk CRUD: PASS"
        }
        finally {
            foreach ($path in @($original, $renamed)) {
                Invoke-CloudCurl @(
                    '-o', 'NUL', '-X', 'DELETE', '-H', "Host: $HostName",
                    '-H', "Cookie: $cookie",
                    "$BaseUrl/api/resources?source=$source&path=$path"
                ) | Out-Null
            }
        }
    }
}
finally {
    Remove-Item -LiteralPath $headersFile, $bodyFile -Force -ErrorAction SilentlyContinue
}
