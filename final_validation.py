import subprocess
import re

configs = ["default", "rush", "service-line", "bake-off", "function", "banquet-night", "brigade", "one-cook", "blind"]

print("=== FINAL VALIDATION ===")
for c in configs:
    print(f"\nProfile: {c}")
    out = subprocess.check_output(f"py -3 launch.py compare second-try --config {c} --seeds 2000..2020", shell=True).decode('utf-8', errors='ignore')
    
    my_sched = ""
    julia = ""
    gordon = ""
    for line in out.splitlines():
        if "my_scheduler" in line:
            my_sched = line
        elif "julia_child" in line:
            julia = line
        elif "gordon_ramsay" in line:
            gordon = line
            
    print(gordon)
    print(julia)
    print(my_sched)
