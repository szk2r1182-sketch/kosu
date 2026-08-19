# ============================================================
# Obsidian 自動保存ツール セットアップ
#
# 使い方: このファイルを右クリック →「PowerShell で実行」
#         うまくいかない場合は README.md の「困ったとき」を見てください。
# ============================================================

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$TaskName  = "ObsidianSync_NotebookLM"

function Write-Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Write-OK($text)       { Write-Host "    OK: $text" -ForegroundColor Green }
function Write-Warn2($text)    { Write-Host "    注意: $text" -ForegroundColor Yellow }

Write-Host "============================================" -ForegroundColor White
Write-Host " Obsidian 自動保存ツール セットアップ" -ForegroundColor White
Write-Host "============================================" -ForegroundColor White

# ------------------------------------------------------------
Write-Step 1 "Python が入っているか確認します"
# ------------------------------------------------------------
$python = $null
foreach ($cmd in @("python", "py")) {
    try {
        $ver = & $cmd --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$ver" -match "Python 3\.(\d+)") {
            if ([int]$Matches[1] -ge 8) {
                $python = (Get-Command $cmd).Source
                Write-OK "$ver ($python)"
                break
            }
        }
    } catch { }
}
if (-not $python) {
    Write-Host @"

    Python が見つかりませんでした。

    1. https://www.python.org/downloads/windows/ を開く
    2. 「Download Python 3.x.x」からインストーラを入手
    3. インストール画面で
       「Add python.exe to PATH」に必ずチェックを入れる
    4. インストール後、PowerShell を閉じて開き直し、
       もう一度この setup.ps1 を実行してください
"@ -ForegroundColor Red
    Read-Host "`nEnter キーで終了します"
    exit 1
}

# 画面が一瞬光らないよう、あれば pythonw を使う
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

# ------------------------------------------------------------
Write-Step 2 "Obsidian の保管庫（OneDrive の中）を探します"
# ------------------------------------------------------------
$vaultName = "アレクサンドリア図書館"
$found = @()
foreach ($root in (Get-ChildItem $env:USERPROFILE -Directory -Filter "OneDrive*" -ErrorAction SilentlyContinue)) {
    $found += Get-ChildItem $root.FullName -Directory -Recurse -Depth 4 -Filter $vaultName -ErrorAction SilentlyContinue
}

$vault = $null
if ($found.Count -eq 1) {
    $vault = $found[0].FullName
    Write-OK "見つかりました: $vault"
} elseif ($found.Count -gt 1) {
    Write-Host "    候補が複数見つかりました。番号を選んでください:" -ForegroundColor Yellow
    for ($i = 0; $i -lt $found.Count; $i++) { Write-Host "      [$i] $($found[$i].FullName)" }
    $sel = Read-Host "    番号"
    $vault = $found[[int]$sel].FullName
} else {
    Write-Warn2 "自動で見つかりませんでした。"
    Write-Host "    エクスプローラーで保管庫のフォルダを開き、アドレス欄のパスを貼り付けてください。"
    Write-Host "    例: C:\Users\自分\OneDrive - SUBARU CORPORATION\ドキュメント\Obsidian\$vaultName"
    $vault = (Read-Host "    保管庫のパス").Trim('"').Trim()
}

if (-not (Test-Path $vault)) {
    Write-Host "`n    そのフォルダが見つかりません: $vault" -ForegroundColor Red
    Write-Host "    OneDrive の同期が終わっているか確認してください。" -ForegroundColor Red
    Read-Host "`nEnter キーで終了します"
    exit 1
}

# ------------------------------------------------------------
Write-Step 3 "取り込みBOX を作ります"
# ------------------------------------------------------------
$inbox = Join-Path ([Environment]::GetFolderPath("Desktop")) "Obsidian取り込みBOX"
New-Item -ItemType Directory -Path $inbox -Force | Out-Null
Write-OK "デスクトップに「Obsidian取り込みBOX」を作りました"
Write-Host "    ここにファイルを入れておくと、自動で Obsidian に保存されます。"

# ------------------------------------------------------------
Write-Step 4 "設定ファイル (config.json) を書き出します"
# ------------------------------------------------------------
$configPath = Join-Path $ScriptDir "config.json"
if (Test-Path $configPath) {
    $answer = Read-Host "    config.json が既にあります。上書きしますか？ (y/N)"
    if ($answer -ne "y") { Write-Warn2 "既存の設定を使います。" }
}
if (-not (Test-Path $configPath) -or $answer -eq "y") {
    $config = [ordered]@{
        inbox_dir         = $inbox
        vault_dir         = $vault
        subfolder         = "Inbox/NotebookLM"
        tags              = @("notebooklm", "inbox")
        archive_inputs    = $true
        filename_template = "{date}_{title}"
        max_title_length  = 80
    }
    $json = $config | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding $false))
    Write-OK "書き出しました: $configPath"
}

# ------------------------------------------------------------
Write-Step 5 "動作テストをします（まだ何も書き込みません）"
# ------------------------------------------------------------
& $python (Join-Path $ScriptDir "sync.py") --dry-run
if ($LASTEXITCODE -ne 0) {
    Write-Host "`n    テストに失敗しました。上のメッセージを確認してください。" -ForegroundColor Red
    Read-Host "`nEnter キーで終了します"
    exit 1
}
Write-OK "問題ありませんでした"

# ------------------------------------------------------------
Write-Step 6 "10分おきの自動実行を登録します"
# ------------------------------------------------------------
$target = "`"$pythonw`" `"$(Join-Path $ScriptDir 'sync.py')`""
schtasks /Create /TN $TaskName /TR $target /SC MINUTE /MO 10 /F 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-OK "登録しました（タスク名: $TaskName）"
    Write-Host "    止めたいときは stop.ps1 を実行してください。"
} else {
    Write-Warn2 "自動実行の登録に失敗しました（会社のPCで禁止されている場合があります）。"
    Write-Host "    その場合は run.bat をダブルクリックして手動で実行してください。"
}

Write-Host "`n============================================" -ForegroundColor Green
Write-Host " セットアップ完了" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host @"

これからの使い方:

  デスクトップの「Obsidian取り込みBOX」に
  ファイル（.md / .txt / .docx / .zip）を入れる
        ↓ 10分以内に自動で
  Obsidian の $vaultName / Inbox / NotebookLM に
  Markdown として保存される

すぐ反映させたいときは run.bat をダブルクリックしてください。

"@
Read-Host "Enter キーで終了します"
