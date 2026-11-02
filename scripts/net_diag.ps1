<#
.SYNOPSIS
    Pre-service network check for the Áron realtime path.

.DESCRIPTION
    The app's own latency readout (the stt / mt / tts / e2e rows) measures each
    provider round trip END TO END -- transport and provider processing in one
    number. When a Sunday runs slow that number cannot say which half moved,
    and the usual answer, "Soniox is having a bad day", is unfalsifiable
    without a transport measurement beside it. This script is that
    measurement.

    For every host the realtime path depends on it reports:
      DNS     name resolution -- a slow resolver shows up here and nowhere else
      TCP     the TCP handshake: one round trip, so this IS the RTT
      TLS     the TLS handshake on top of it (~2 further round trips)
      Avg/Max/Jitter  over -Count repeated connects
      Loss    connects that failed or timed out

    Measured over TCP rather than ICMP deliberately: the provider edges
    deprioritise or drop ping, so a ping figure for them is either missing or
    meaningless, while port 443 is the port the app actually uses. The
    reference addresses are pinged, because there ICMP is honest.

    Three reference points localise the blame:
      gateway   the default gateway. LAN and wifi only -- bad here is a cable
                or an access point, not the ISP.
      1.1.1.1   stable anycast, ICMP. Bad here with a healthy gateway is the
                ISP.
      providers the hosts above. Bad here with a healthy gateway AND a healthy
                1.1.1.1 is the provider or the route to it, not your line.

    What the numbers mean for this app: the STT socket is the sensitive one.
    Jitter on stt-rt.soniox.com matters more than raw latency, because a late
    token message delays the '<end>' the sentence assembler cuts on -- so a
    jitter spike reads downstream as the room waiting, not as a network error.
    Any sustained loss is worth chasing; raw RTT under ~60ms is fine.

    Hosts come from the code (Soniox STT/TTS, Anthropic, ElevenLabs) plus
    whatever .env configures (CT_PUBLISH_URL, CT_PUBLISH_CLIP_URL,
    CT_PROPRESENTER_URL), so the caption relay and ProPresenter are checked at
    the addresses this install really uses rather than hardcoded ones. No
    secret is read from .env -- only the host-bearing keys.

.PARAMETER Count
    TCP samples per host, for the jitter figure. Default 10.

.PARAMETER Ping
    ICMP echoes per reference address. Default 50.

.PARAMETER EnvFile
    Path to .env. Defaults to the repo root beside this script.

.PARAMETER Watch
    Instead of one pass, print a timestamped line per interval until Ctrl+C --
    for running through a live service in a second window, next to the app's
    own latency readout.

.PARAMETER Interval
    Seconds between -Watch samples. Default 2.

.PARAMETER TimeoutMs
    Per-connect timeout. Default 5000. Without one a dead host blocks for the
    OS default (~21s), which is no use five minutes before a service.

.EXAMPLE
    .\scripts\net_diag.ps1
    One pass. Run it before a service.

.EXAMPLE
    .\scripts\net_diag.ps1 -Watch
    Live monitor. Spikes that line up with the app's stt row are transport,
    not the provider.

.NOTES
    Windows PowerShell 5.1 and PowerShell 7 both. No curl, no modules: all
    timing is .NET, which sidesteps the 5.1 `curl` alias (it resolves to
    Invoke-WebRequest, which has no -w) and any locale decimal-separator
    trouble in curl's -w output.

    Deliberately no Set-StrictMode and no global $ErrorActionPreference='Stop':
    a diagnostic should limp and report what it managed to measure, not abort
    half way through because one leg is unreachable -- which on a bad day is
    the very thing being diagnosed.
#>
[CmdletBinding()]
param(
    [int]$Count = 10,
    [int]$Ping = 50,
    [string]$EnvFile,
    [switch]$Watch,
    [int]$Interval = 2,
    [int]$TimeoutMs = 5000
)

# ---- .env ------------------------------------------------------------------
# Same shape the app reads: KEY=value a line, # comments, blank lines.
function Get-DotEnvMap {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in (Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue)) {
        $t = $line.Trim()
        if ($t -eq '' -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $k = $t.Substring(0, $i).Trim()
        $v = $t.Substring($i + 1).Trim()
        # Strip a trailing inline comment (the .env.example uses them), then quotes.
        if ($v -notmatch '^["'']') { $v = ($v -split '\s+#')[0].Trim() }
        $v = $v -replace '^"(.*)"$', '$1'
        $v = $v -replace "^'(.*)'$", '$1'
        $map[$k] = $v
    }
    return $map
}

function New-Target {
    param([string]$Name, [string]$HostName, [int]$Port, [bool]$Tls, [string]$Note)
    return [pscustomobject]@{
        Name = $Name; HostName = $HostName; Port = $Port; Tls = $Tls; Note = $Note
    }
}

# The fixed four come from app/server.py: SONIOX_WS_URL, the Soniox TTS host the
# prewarm logs, EL_API, and the Anthropic SDK's default host.
function Get-Targets {
    param([hashtable]$Settings)
    $list = New-Object System.Collections.ArrayList
    [void]$list.Add((New-Target 'soniox-stt' 'stt-rt.soniox.com' 443 $true  'STT websocket - the sensitive one'))
    [void]$list.Add((New-Target 'soniox-tts' 'tts-rt.soniox.com' 443 $true  'Soniox TTS'))
    [void]$list.Add((New-Target 'anthropic'  'api.anthropic.com' 443 $true  'translation'))
    [void]$list.Add((New-Target 'elevenlabs' 'api.elevenlabs.io' 443 $true  'ElevenLabs TTS'))

    $fromEnv = @(
        @{ Key = 'CT_PUBLISH_URL';      Name = 'web-relay';    Note = 'caption relay (background)' }
        @{ Key = 'CT_PUBLISH_CLIP_URL'; Name = 'web-clips';    Note = 'clip relay (background)' }
        @{ Key = 'CT_PROPRESENTER_URL'; Name = 'propresenter'; Note = 'LAN - not the ISP' }
    )
    foreach ($spec in $fromEnv) {
        $raw = $null
        if ($Settings.ContainsKey($spec.Key)) { $raw = $Settings[$spec.Key] }
        if ([string]::IsNullOrWhiteSpace($raw)) { continue }
        $uri = $null
        try { $uri = [System.Uri]$raw } catch { continue }
        if (-not $uri.Host) { continue }
        # CT_PUBLISH_CLIP_URL is derived from CT_PUBLISH_URL by default, so the two
        # are normally the same host; probing it twice tells you nothing.
        $dup = $false
        foreach ($t in $list) { if ($t.HostName -eq $uri.Host -and $t.Port -eq $uri.Port) { $dup = $true } }
        if ($dup) { continue }
        [void]$list.Add((New-Target $spec.Name $uri.Host $uri.Port ($uri.Scheme -eq 'https') $spec.Note))
    }
    return $list
}

# ---- primitives ------------------------------------------------------------
# DNS is resolved separately and the connect made to the ADDRESS, so the TCP
# figure is a handshake and nothing else. Resolving inside BeginConnect would
# fold the resolver's time into it and hide a slow resolver.
function Resolve-Timed {
    param([string]$HostName)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $addrs = [System.Net.Dns]::GetHostAddresses($HostName)
        $sw.Stop()
        $ip = $null
        foreach ($a in $addrs) { if ($a.AddressFamily -eq 'InterNetwork') { $ip = $a; break } }
        if (-not $ip -and $addrs.Count) { $ip = $addrs[0] }
        return [pscustomobject]@{ Ms = $sw.Elapsed.TotalMilliseconds; Address = $ip; Error = $null }
    } catch {
        $sw.Stop()
        return [pscustomobject]@{ Ms = $sw.Elapsed.TotalMilliseconds; Address = $null; Error = $_.Exception.Message }
    }
}

function Connect-Timed {
    param([System.Net.IPAddress]$Address, [int]$Port, [int]$Timeout)
    $client = New-Object System.Net.Sockets.TcpClient
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $iar = $client.BeginConnect($Address, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($Timeout, $false)) {
            $sw.Stop(); $client.Close()
            return [pscustomobject]@{ Ms = $null; Client = $null; Error = 'timeout' }
        }
        $client.EndConnect($iar)
        $sw.Stop()
        return [pscustomobject]@{ Ms = $sw.Elapsed.TotalMilliseconds; Client = $client; Error = $null }
    } catch {
        $sw.Stop()
        if ($client) { $client.Dispose() }
        return [pscustomobject]@{ Ms = $null; Client = $null; Error = $_.Exception.Message }
    }
}

# Normal certificate validation on purpose: a handshake that fails here is
# itself the finding (a captive portal, or a middlebox intercepting TLS between
# the building and the provider), and accepting any certificate would hide
# exactly that.
function Measure-TlsHandshake {
    param([System.Net.Sockets.TcpClient]$Client, [string]$HostName)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $ssl = $null
    try {
        $ssl = New-Object System.Net.Security.SslStream($Client.GetStream(), $false)
        $ssl.AuthenticateAsClient($HostName)
        $sw.Stop()
        return [pscustomobject]@{ Ms = $sw.Elapsed.TotalMilliseconds; Error = $null }
    } catch {
        $sw.Stop()
        return [pscustomobject]@{ Ms = $null; Error = $_.Exception.Message }
    } finally {
        if ($ssl) { $ssl.Dispose() }
    }
}

function Measure-Target {
    param([pscustomobject]$Target, [int]$Samples, [int]$Timeout)

    $dns = Resolve-Timed -HostName $Target.HostName
    $dnsMs = [math]::Round($dns.Ms, 1)
    if (-not $dns.Address) {
        return [pscustomobject]@{
            Name = $Target.Name; HostName = $Target.HostName; DNS = $dnsMs
            TCP = $null; TLS = $null; Avg = $null; Max = $null; Jitter = $null
            Loss = $Samples; Status = "dns: $($dns.Error)"
        }
    }

    # The first connect carries the TLS measurement; the rest are RTT samples.
    # One handshake is enough -- TLS cost does not vary the way RTT does, and a
    # handshake per sample would be needless load on the provider.
    $status = 'ok'
    $tlsMs = $null
    $first = Connect-Timed -Address $dns.Address -Port $Target.Port -Timeout $Timeout
    if ($first.Client) {
        if ($Target.Tls) {
            $tls = Measure-TlsHandshake -Client $first.Client -HostName $Target.HostName
            $tlsMs = $tls.Ms
            if ($tls.Error) { $status = "tls: $($tls.Error)" }
        }
        $first.Client.Dispose()
    } else {
        $status = "tcp: $($first.Error)"
    }

    $times = New-Object System.Collections.ArrayList
    if ($null -ne $first.Ms) { [void]$times.Add([double]$first.Ms) }
    for ($i = 1; $i -lt $Samples; $i++) {
        $c = Connect-Timed -Address $dns.Address -Port $Target.Port -Timeout $Timeout
        if ($c.Client) { $c.Client.Dispose() }
        if ($null -ne $c.Ms) { [void]$times.Add([double]$c.Ms) }
    }

    # Built in steps rather than inline: an `if` is a statement, and a statement
    # is a parse error as a hashtable literal's value on PowerShell 5.1.
    $avg = $null; $max = $null; $jitter = $null
    if ($times.Count -gt 0) {
        $s = $times | Measure-Object -Minimum -Maximum -Average
        $avg = [math]::Round($s.Average, 1)
        $max = [math]::Round($s.Maximum, 1)
        $jitter = [math]::Round($s.Maximum - $s.Minimum, 1)
    }
    $tcpMs = $null
    if ($null -ne $first.Ms) { $tcpMs = [math]::Round($first.Ms, 1) }
    if ($null -ne $tlsMs) { $tlsMs = [math]::Round($tlsMs, 1) }

    return [pscustomobject]@{
        Name = $Target.Name; HostName = $Target.HostName; DNS = $dnsMs
        TCP = $tcpMs; TLS = $tlsMs; Avg = $avg; Max = $max; Jitter = $jitter
        Loss = ($Samples - $times.Count); Status = $status
    }
}

# System.Net.NetworkInformation.Ping rather than Test-Connection: the cmdlet
# renamed its parameters and its latency property between 5.1 and 7, and this
# has to run on both.
function Measure-Icmp {
    param([string]$Target, [int]$Echoes, [int]$Timeout)
    $pinger = New-Object System.Net.NetworkInformation.Ping
    $times = New-Object System.Collections.ArrayList
    $lost = 0
    for ($i = 0; $i -lt $Echoes; $i++) {
        try {
            $r = $pinger.Send($Target, $Timeout)
            if ($r -and $r.Status -eq 'Success') { [void]$times.Add([double]$r.RoundtripTime) }
            else { $lost++ }
        } catch { $lost++ }
    }
    $pinger.Dispose()

    $min = $null; $avg = $null; $max = $null; $jitter = $null
    if ($times.Count -gt 0) {
        $s = $times | Measure-Object -Minimum -Maximum -Average
        $min = [math]::Round($s.Minimum, 1)
        $avg = [math]::Round($s.Average, 1)
        $max = [math]::Round($s.Maximum, 1)
        $jitter = [math]::Round($s.Maximum - $s.Minimum, 1)
    }
    $lossPct = $null
    if ($Echoes -gt 0) { $lossPct = [math]::Round((100.0 * $lost / $Echoes), 1) }

    return [pscustomobject]@{
        Target = $Target; Min = $min; Avg = $avg; Max = $max
        Jitter = $jitter; Lost = $lost; LossPct = $lossPct
    }
}

function Get-DefaultGatewayAddress {
    try {
        $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
                 Sort-Object RouteMetric | Select-Object -First 1
        if ($route -and $route.NextHop -and $route.NextHop -ne '0.0.0.0') { return $route.NextHop }
    } catch { }
    # Get-NetRoute is Win8+; fall back to parsing ipconfig.
    try {
        foreach ($line in (ipconfig)) {
            if ($line -match 'Gateway' -and $line -match '(\d{1,3}(?:\.\d{1,3}){3})') {
                if ($Matches[1] -ne '0.0.0.0') { return $Matches[1] }
            }
        }
    } catch { }
    return $null
}

function Format-Ms {
    param($Value)
    if ($null -eq $Value) { return 'LOSS  ' }
    return ('{0,6:N1}' -f $Value)
}

# ---- main ------------------------------------------------------------------
if (-not $EnvFile) {
    # $PSCommandPath is empty when the body is pasted into a console rather than
    # run as a file, so fall back to the working directory instead of throwing.
    if ($PSCommandPath) { $repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath) }
    else { $repoRoot = (Get-Location).Path }
    $EnvFile = Join-Path $repoRoot '.env'
}
$settings = Get-DotEnvMap -Path $EnvFile
$targets = Get-Targets -Settings $settings

$envLabel = $EnvFile
if (-not (Test-Path -LiteralPath $EnvFile)) { $envLabel = "$EnvFile (not found - provider hosts only)" }

Write-Host ''
Write-Host 'Aron network diagnostic' -ForegroundColor Cyan
Write-Host ("  .env   {0}" -f $envLabel)
Write-Host ("  host   {0}   PowerShell {1}" -f $env:COMPUTERNAME, $PSVersionTable.PSVersion)
Write-Host ''

if ($Watch) {
    # Live mode. One line per interval: the STT socket (the one that matters)
    # plus the two reference points, so a spike can be placed the moment it
    # happens. Single samples -- the trend down the column is the measurement.
    $gateway = Get-DefaultGatewayAddress
    $gwLabel = 'n/a'
    if ($gateway) { $gwLabel = $gateway }
    Write-Host ("Watching every {0}s. Ctrl+C to stop." -f $Interval) -ForegroundColor Cyan
    Write-Host ("  stt = stt-rt.soniox.com:443 (TCP)    gw = {0} (ICMP)    ref = 1.1.1.1 (ICMP)" -f $gwLabel)
    Write-Host ''
    Write-Host 'time          stt        gw       ref'
    $sttIp = (Resolve-Timed -HostName 'stt-rt.soniox.com').Address
    while ($true) {
        $sttMs = $null
        if ($sttIp) {
            $c = Connect-Timed -Address $sttIp -Port 443 -Timeout $TimeoutMs
            if ($c.Client) { $c.Client.Dispose() }
            $sttMs = $c.Ms
        }
        $gwMs = $null
        if ($gateway) { $gwMs = (Measure-Icmp -Target $gateway -Echoes 1 -Timeout $TimeoutMs).Avg }
        $refMs = (Measure-Icmp -Target '1.1.1.1' -Echoes 1 -Timeout $TimeoutMs).Avg

        $line = '{0:HH:mm:ss}   {1}  {2}  {3}' -f (Get-Date), (Format-Ms $sttMs), (Format-Ms $gwMs), (Format-Ms $refMs)
        # Colour only what is worth looking up for: a lost or slow STT connect.
        if ($null -eq $sttMs) { Write-Host $line -ForegroundColor Red }
        elseif ($sttMs -gt 150) { Write-Host $line -ForegroundColor Yellow }
        else { Write-Host $line }
        Start-Sleep -Seconds $Interval
    }
}

Write-Host ("Providers   ({0} TCP samples each, ms)" -f $Count) -ForegroundColor Cyan
$results = @()
foreach ($t in $targets) { $results += (Measure-Target -Target $t -Samples $Count -Timeout $TimeoutMs) }
$results | Format-Table -AutoSize -Property Name, HostName, DNS, TCP, TLS, Avg, Max, Jitter, Loss, Status

Write-Host ("Reference   ({0} ICMP echoes each, ms)" -f $Ping) -ForegroundColor Cyan
$gateway = Get-DefaultGatewayAddress
$refTargets = @()
if ($gateway) { $refTargets += $gateway }
else { Write-Host '  (no default gateway found - skipping the LAN leg)' }
$refTargets += '1.1.1.1'
$refTargets += '8.8.8.8'
$icmp = @()
foreach ($r in $refTargets) { $icmp += (Measure-Icmp -Target $r -Echoes $Ping -Timeout $TimeoutMs) }
$icmp | Format-Table -AutoSize -Property Target, Min, Avg, Max, Jitter, Lost, LossPct

# ---- reading it ------------------------------------------------------------
# The thresholds are deliberately coarse: they exist to say "look here", not to
# grade the line. Ordered outward from the building, because a bad LAN makes
# every number past it bad too and chasing the ISP first wastes the morning.
Write-Host 'Reading it' -ForegroundColor Cyan

$stt = $null
foreach ($r in $results) { if ($r.Name -eq 'soniox-stt') { $stt = $r } }
$lan = $null
if ($gateway) { foreach ($r in $icmp) { if ($r.Target -eq $gateway) { $lan = $r } } }
$isp = $null
foreach ($r in $icmp) { if ($r.Target -eq '1.1.1.1') { $isp = $r } }

if ($lan -and (($lan.LossPct -gt 0) -or ($lan.Max -gt 20))) {
    Write-Host ('  LAN: gateway shows {0}% loss, max {1}ms. Fix the cable or the access point before blaming anything upstream.' -f $lan.LossPct, $lan.Max) -ForegroundColor Yellow
} elseif ($isp -and (($isp.LossPct -gt 0) -or ($isp.Jitter -gt 30))) {
    Write-Host ('  ISP: 1.1.1.1 shows {0}% loss and {1}ms jitter with a healthy gateway. That is the line, not the providers.' -f $isp.LossPct, $isp.Jitter) -ForegroundColor Yellow
} elseif ($stt -and (($stt.Loss -gt 0) -or ($stt.Jitter -gt 30))) {
    Write-Host ('  STT path: {0} lost connect(s) and {1}ms jitter to stt-rt.soniox.com, with the line healthy. Provider or the route to it.' -f $stt.Loss, $stt.Jitter) -ForegroundColor Yellow
} else {
    Write-Host '  Nothing stands out. If a service still runs slow, the time is provider-side:' -ForegroundColor Green
    Write-Host "  subtract the TLS column above from the app's stt / mt / tts rows and the remainder is their processing."
}

Write-Host ''
Write-Host '  If a leg looks bad, next:   pathping -n -q 50 stt-rt.soniox.com'
Write-Host '  Hop 1 is your router. Loss from hop 2 onward that persists downstream is the ISP;'
Write-Host '  loss at a single hop that clears below it is only ICMP deprioritisation - ignore it.'
Write-Host ''
