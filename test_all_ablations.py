import os
import subprocess
import shutil

original_file = 'second-try/scheduler.py'
backup_file = 'second-try/scheduler.py.bak'
shutil.copy(original_file, backup_file)

with open(original_file, 'r') as f:
    code = f.read()

# Apply both fixes
code = code.replace("AGE_PROMOTE = 3.0", "AGE_PROMOTE = 1e9")
code = code.replace("IO_FACTOR = 0.5", "IO_FACTOR = 1.0")

with open(original_file, 'w') as f:
    f.write(code)

configs = ["default", "rush", "service-line", "bake-off", "function", "banquet-night", "brigade", "one-cook", "blind"]

print("=== HOLD OUT SET with AGE_PROMOTE=1e9, IO_FACTOR=1.0 ===")
for c in configs:
    print(f"\nProfile: {c}")
    subprocess.run(f"py -3 launch.py compare second-try --config {c} --seeds 2000..2020 | findstr my_scheduler", shell=True)

shutil.copy(backup_file, original_file)
print("\nDone!")
