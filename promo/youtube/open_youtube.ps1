# Open an isolated browser session for the owner's YouTube sign-in and uploads.
# This profile lives outside the repository and is never copied or exported.
$chromePath = Join-Path ${env:ProgramFiles} 'Google\Chrome\Application\chrome.exe'
if (-not (Test-Path -LiteralPath $chromePath)) {
    throw 'Google Chrome introuvable.'
}
$promoProfile = Join-Path $env:LOCALAPPDATA 'blink2video-youtube-browser'
$chromeArguments = @(
    ('--user-data-dir="' + $promoProfile + '"'),
    '--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=9222',
    '--no-first-run',
    '--no-default-browser-check',
    'https://www.youtube.com/channel_switcher'
)
# Visible because the owner must sign in personally; no credentials are read.
Start-Process -FilePath $chromePath -ArgumentList $chromeArguments -WindowStyle Normal
