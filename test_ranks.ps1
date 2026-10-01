 = @(
    "wr * wuw",
    "(wr ** 2) * wuw",
    "wr * (wuw ** 2)",
    "wuw",
    "(wr ** 2) * 100 + wuw"
)

foreach ($f in $formulas) {
    Write-Host "--- Formula: $f ---"
    (Get-Content my-scheduler/scheduler.py) -replace 'base = .*', ("base = " + $f) | Set-Content my-scheduler/scheduler.py
    py -3 launch.py compare my-scheduler --no-open 2>&1 | Select-String "my_scheduler"
}
