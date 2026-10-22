# Optional Windows trust-chain transport. No disabled certificate checks, no
# redirects, credentials, cookies or file downloads. Input/output are JSON pipes.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$newsInput = [Console]::In.ReadToEnd() | ConvertFrom-Json
$newsHandler = $null
$newsClient = $null
try {
    Add-Type -AssemblyName System.Net.Http
    $newsHandler = New-Object System.Net.Http.HttpClientHandler
    $newsHandler.AllowAutoRedirect = $false
    $newsHandler.UseCookies = $false
    $newsHandler.UseDefaultCredentials = $false
    $newsClient = New-Object System.Net.Http.HttpClient($newsHandler)
    $newsClient.Timeout = [TimeSpan]::FromSeconds(15)
    foreach ($newsHeader in $newsInput.headers.PSObject.Properties) {
        [void]$newsClient.DefaultRequestHeaders.TryAddWithoutValidation($newsHeader.Name,[string]$newsHeader.Value)
    }
    $newsResponse = $newsClient.GetAsync([string]$newsInput.url,[System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
    $newsHeaders = @{}
    foreach ($newsHeader in $newsResponse.Headers) { $newsHeaders[$newsHeader.Key] = $newsHeader.Value -join ', ' }
    foreach ($newsHeader in $newsResponse.Content.Headers) { $newsHeaders[$newsHeader.Key] = $newsHeader.Value -join ', ' }
    $newsStream = $newsResponse.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
    $newsBuffer = New-Object byte[] 16384
    $newsMemory = New-Object System.IO.MemoryStream
    $newsClock = [Diagnostics.Stopwatch]::StartNew()
    while (($newsRead = $newsStream.Read($newsBuffer,0,$newsBuffer.Length)) -gt 0) {
        if ($newsMemory.Length + $newsRead -gt [int]$newsInput.limit -or $newsClock.Elapsed.TotalSeconds -gt 20) { throw 'Response limit' }
        $newsMemory.Write($newsBuffer,0,$newsRead)
    }
    @{ status=[int]$newsResponse.StatusCode; headers=$newsHeaders; body=[Convert]::ToBase64String($newsMemory.ToArray()) } | ConvertTo-Json -Depth 4 -Compress
    $newsStream.Dispose()
    $newsMemory.Dispose()
    $newsResponse.Dispose()
} catch {
    @{error='Windows HTTPS verification or bounded request failed'} | ConvertTo-Json -Compress
} finally {
    if ($newsClient) { $newsClient.Dispose() }
    if ($newsHandler) { $newsHandler.Dispose() }
}
