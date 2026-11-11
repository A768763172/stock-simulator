import subprocess, os
out = []
try:
    r = subprocess.run("wmic process where \"name='python.exe'\" get ProcessId,CommandLine /FORMAT:CSV",
                       capture_output=True, timeout=30, shell=True)
    txt = (r.stdout or b"").decode("gbk", errors="replace")
    out.append(txt)
except Exception as e:
    out.append("ERR " + str(e))
with open(os.path.join(os.path.dirname(__file__), "procs.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("written", len(out[0]))
