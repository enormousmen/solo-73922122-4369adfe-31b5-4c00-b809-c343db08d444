import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# 模块级 app 实例使用的默认库指向临时文件，避免污染仓库目录
os.environ.setdefault(
    "OCI_GC_DB", str(Path(tempfile.gettempdir()) / "ocigc-module-default.db")
)
