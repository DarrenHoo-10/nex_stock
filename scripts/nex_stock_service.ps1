# nex_stock 常驻服务管理脚本（Windows）
#
# 用法（PowerShell）：
#   .\scripts\nex_stock_service.ps1 install    # 注册开机自启计划任务 + 防火墙规则，并立即启动（需要 UAC 授权）
#   .\scripts\nex_stock_service.ps1 uninstall  # 停止并注销计划任务、删除防火墙规则（需要 UAC 授权）
#   .\scripts\nex_stock_service.ps1 start      # 通过计划任务启动 MCP 管家
#   .\scripts\nex_stock_service.ps1 stop       # 停止管家与分析服务
#   .\scripts\nex_stock_service.ps1 status     # 查看管家/分析服务/计划任务状态
#
# 架构：计划任务登录自启一个看门狗 PowerShell（while 循环守护 MCP 管家，:8800）；
#       管家负责拉起/停止分析服务（python main.py --serve-only, :8000）。
#       管家进程意外退出时由看门狗 5 秒内自动重启。
# 注意：本文件必须保存为 UTF-8 with BOM（Windows PowerShell 5.1 兼容）。

param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "uninstall", "start", "stop", "status")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$TaskName = "NexStockMCP"
$FirewallRuleName = "nex-stock-mcp-8800"
# ZeroTier 网段：只放行虚拟网内访问（Nex 网关服务器经 ZeroTier 回连）
$ZeroTierSubnet = "10.73.0.0/16"
# 看门狗进程标记：用于在进程列表中定位看门狗 PowerShell
$WatchdogMarker = "NexStockMCP-watchdog"

function Test-IsElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-RepoEnvValue([string]$Name) {
    $line = Select-String -Path (Join-Path $RepoRoot ".env") -Pattern ("^" + $Name + "=") -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($line) { return ($line.Line -split "=", 2)[1].Trim() }
    return ""
}

function Get-McpProcesses {
    # 管家 python 进程 + 看门狗 PowerShell 进程（命令行特征 + 8800 端口反查双策略；
    # 提升上下文里运行的进程对非提升查询隐藏 CommandLine，端口反查是兜底）
    $list = @()
    $list += Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -match "mcp_server" }
    $list += Get-CimInstance Win32_Process -Filter "Name like 'powershell%'" |
        Where-Object { $_.CommandLine -like "*$WatchdogMarker*" }
    $portPid = 0
    netstat -ano | Select-String ":8800.*LISTENING" | ForEach-Object {
        try { $portPid = [int](($_.ToString().Trim() -split "\s+")[-1]) } catch { }
    }
    if ($portPid -gt 0) {
        $byPort = Get-CimInstance Win32_Process -Filter "ProcessId=$portPid"
        if ($byPort -and $byPort.Name -like "python*") {
            $known = $list | Where-Object { $_.ProcessId -eq $portPid }
            if (-not $known) { $list += $byPort }
        }
    }
    return $list | Sort-Object ProcessId -Unique
}

function Invoke-Install {
    $token = Get-RepoEnvValue "MCP_SERVER_BEARER_TOKEN"
    if (-not $token) { throw ".env 缺少 MCP_SERVER_BEARER_TOKEN（对外服务必须 Bearer 认证）" }

    Write-Host "[1/3] 注册计划任务（登录自启 + 看门狗守护）..."
    $watchdogCmd = "# $WatchdogMarker`nSet-Location '$RepoRoot'; while (`$true) { & '$VenvPython' -m mcp_server; Start-Sleep -Seconds 5 }"
    $taskAction = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$watchdogCmd`"" `
        -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    # RunLevel=Limited：管家不需要管理员权限；非提升 shell 也能查看/终止该进程
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "nex-stock MCP 管家（托管股票分析服务常驻进程）" -Force | Out-Null

    Write-Host "[2/3] 配置防火墙（放行 8800，仅 ZeroTier 网段 $ZeroTierSubnet）..."
    netsh advfirewall firewall delete rule name="$FirewallRuleName" > $null 2>&1
    netsh advfirewall firewall add rule name="$FirewallRuleName" dir=in action=allow protocol=TCP localport=8800 remoteip=$ZeroTierSubnet | Out-Null

    Write-Host "[3/3] 启动计划任务..."
    schtasks /run /tn $TaskName | Out-Null
    Start-Sleep -Seconds 6
    Invoke-Status
}

function Invoke-Uninstall {
    Write-Host "[1/2] 停止看门狗、管家与分析服务..."
    Get-McpProcesses | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    & $VenvPython -c "import sys; sys.path.insert(0, r'$RepoRoot'); from mcp_server import service; print('分析服务:', service.stop_service())" 2>$null
    Write-Host "[2/2] 注销计划任务与防火墙规则..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    netsh advfirewall firewall delete rule name="$FirewallRuleName" > $null 2>&1
    Write-Host "完成。"
}

function Invoke-Start {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 6
    Invoke-Status
}

function Invoke-Stop {
    $procs = Get-McpProcesses
    if ($procs) {
        $procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Host "看门狗与管家已停止。"
    } else {
        Write-Host "管家未在运行。"
    }
    & $VenvPython -c "import sys; sys.path.insert(0, r'$RepoRoot'); from mcp_server import service; print('分析服务:', service.stop_service())" 2>$null
}

function Invoke-Status {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $taskState = if ($task) { "$($task.State)" } else { "未注册" }
    $mcp = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -match "mcp_server" }
    $mcpState = if ($mcp) { "运行中 (pid=$($mcp.ProcessId -join ','))" } else { "未运行" }
    $svc = & $VenvPython -c "import sys; sys.path.insert(0, r'$RepoRoot'); from mcp_server import service; s=service.get_service_status(); print('健康' if s['healthy'] else '未运行')" 2>$null
    Write-Host "计划任务 [$TaskName]: $taskState"
    Write-Host "MCP 管家 (:8800): $mcpState"
    Write-Host "分析服务 (:8000): $svc"
}

# install/uninstall 需要提升权限（计划任务 + 防火墙）；非提升时自动弹 UAC 重跑
if ($Action -in @("install", "uninstall") -and -not (Test-IsElevated)) {
    Write-Host "需要管理员权限，正在弹出 UAC 授权窗口（请在弹窗中点击「是」）..."
    Start-Process powershell -Verb RunAs -Wait -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", $Action
    )
    Start-Sleep -Seconds 2
    Invoke-Status
    exit 0
}

switch ($Action) {
    "install" { Invoke-Install }
    "uninstall" { Invoke-Uninstall }
    "start" { Invoke-Start }
    "stop" { Invoke-Stop }
    "status" { Invoke-Status }
}
