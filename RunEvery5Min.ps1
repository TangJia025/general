# 设置执行策略为 Bypass 时仍需显式指定路径
$pythonExe = "C:\Users\t00969236\AppData\Local\Programs\Python\Python312\python.exe" # <-- 请替换为你的实际 python 路径
$scriptFile = "C:\Users\t00969236\Downloads\xlb-notify\xlb_notify.py"

while ($true) {
    $logTime = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$logTime] 开始执行 xlb_notify.py..." -ForegroundColor Green

    try {
        # 使用 & 调用 Python 执行脚本
        & $pythonExe $scriptFile
        $exitCode = $LASTEXITCODE
        
        if ($exitCode -eq 0) {
            Write-Host "[$logTime] 执行成功。" -ForegroundColor Green
        } else {
            Write-Host "[$logTime] 执行失败，退出码: $exitCode" -ForegroundColor Red
        }
    } catch {
        Write-Host "[$logTime] 执行异常: $_" -ForegroundColor Red
    }

    Write-Host "[$logTime] 等待 5 分钟..." -ForegroundColor Yellow
    Start-Sleep -Seconds 300
}
