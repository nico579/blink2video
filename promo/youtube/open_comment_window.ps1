$chromePath = Join-Path ${env:ProgramFiles} 'Google\Chrome\Application\chrome.exe'
$promoProfile = Join-Path $env:LOCALAPPDATA 'blink2video-youtube-browser'
Start-Process -FilePath $chromePath -ArgumentList @(
    ('--user-data-dir="' + $promoProfile + '"'),
    '--new-window',
    'https://www.youtube.com/watch?v=6rNqLI9K8Tc'
) -WindowStyle Normal
