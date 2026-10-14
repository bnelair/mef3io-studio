# Run a packaged mef3io-studio.exe with --self-test (or --self-test-gui) and
# fail unless it reports OK. The exe is a GUI (windowed) app, so PowerShell
# would not wait for it; Start-Process -Wait does, and the result line is also
# written to a report file in case stdout is not available to a windowed process.
param(
  [Parameter(Mandatory = $true)][string]$Exe,
  [switch]$Gui
)
$ErrorActionPreference = "Stop"
$mode = if ($Gui) { "--self-test-gui" } else { "--self-test" }
$report = Join-Path $env:RUNNER_TEMP ("selftest-" + [guid]::NewGuid() + ".txt")
$stdout = "$report.stdout"
$stderr = "$report.stderr"
$p = Start-Process -FilePath $Exe -ArgumentList $mode, "--self-test-report", "`"$report`"" `
     -Wait -PassThru -NoNewWindow -RedirectStandardOutput $stdout -RedirectStandardError $stderr
Write-Host "exit code: $($p.ExitCode)"
Write-Host "stdout: $(Get-Content $stdout -Raw -ErrorAction SilentlyContinue)"
Write-Host "stderr: $(Get-Content $stderr -Raw -ErrorAction SilentlyContinue)"
if (-not (Test-Path $report)) { throw "self-test wrote no report" }
$line = (Get-Content $report -Raw).Trim()
Write-Host "report: $line"
if ($p.ExitCode -ne 0) { throw "self-test exited with $($p.ExitCode)" }
if (-not $line.StartsWith("mef3io-studio self-test OK")) { throw "self-test did not report OK" }
if ($Gui -and -not $line.Contains("display windows")) { throw "GUI self-test did not use the windows display plugin" }
