import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))
from locale_release.contracts import BaseCourseVersion, LocaleOverride

entity = BaseCourseVersion("E-DEMO", "区域课程本地化发布", 1)
record = LocaleOverride("R-DEMO", entity.entity_id, "已登记")
print(json.dumps({"entity": entity.display_name, "revision": entity.revision, "record_state": record.category}, ensure_ascii=False))
