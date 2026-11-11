import os, sys, unittest, io, contextlib

os.chdir(r'C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator')
sys.path.insert(0, '.')

loader = unittest.TestLoader()
suite = loader.discover('tests', pattern='test_*.py')
buf = io.StringIO()
r = unittest.TextTestRunner(verbosity=1, stream=buf).run(suite)
out = buf.getvalue()

# 列出失败/错误详情
details = []
for t, msg in (r.failures + r.errors)[:10]:
    details.append('--- ' + str(t) + ' ---\n' + msg[:800])

result = (
    f"TOTAL={r.testsRun} FAILURES={len(r.failures)} ERRORS={len(r.errors)}\n"
    f"{'ALL_PASS' if (r.failures or r.errors) == () else 'HAS_ISSUES'}\n"
    f"\n=== STDOUT TAIL ===\n" + out[-1500:] +
    (("\n=== DETAILS ===\n" + "\n".join(details)) if details else "")
)
with open(r'C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator\data_source_verify\test_result.txt', 'w', encoding='utf-8') as f:
    f.write(result)
print(result)
