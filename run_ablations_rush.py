import os
import subprocess
import shutil

original_file = 'second-try/scheduler.py'
backup_file = 'second-try/scheduler.py.bak'
shutil.copy(original_file, backup_file)

with open(original_file, 'r') as f:
    code = f.read()

ablations = [
    ("PREEMPT_ENABLED = True", "PREEMPT_ENABLED = False"),
    ("LAMBDA = 0.5", "LAMBDA = 0"),
    ("AGE_PROMOTE = 3.0", "AGE_PROMOTE = 1e9"),
    ("FIRST_SLICE_ANY_SIZE = True", "FIRST_SLICE_ANY_SIZE = False"),
    ("IO_FACTOR = 0.5", "IO_FACTOR = 1.0"),
]

print("Baseline rush:")
subprocess.run("py -3 launch.py compare second-try --config rush --seeds 2000..2010 | findstr my_scheduler", shell=True)

for target, replacement in ablations:
    print(f"\nAblation: {replacement}")
    new_code = code.replace(target, replacement)
    with open(original_file, 'w') as f:
        f.write(new_code)
    
    subprocess.run("py -3 launch.py compare second-try --config rush --seeds 2000..2010 | findstr my_scheduler", shell=True)

shutil.copy(backup_file, original_file)
print("\nDone!")
