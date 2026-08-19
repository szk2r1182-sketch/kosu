# 自動実行を止めます（ファイルは消えません）
$TaskName = "ObsidianSync_NotebookLM"
schtasks /Delete /TN $TaskName /F
Write-Host "`n自動実行を止めました。再開したいときは setup.ps1 をもう一度実行してください。" -ForegroundColor Green
Read-Host "Enter キーで終了します"
