$helpers = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'youtube_comment_fr\.py prepare en|browser_status\.py'
}
$helpers | Select-Object ProcessId, CommandLine
$helpers | ForEach-Object { Stop-Process -Id $_.ProcessId }
