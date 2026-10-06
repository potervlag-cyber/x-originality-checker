"""Check the distributed template itself, so help text cannot contaminate sources."""
import json
import re
import unittest
from pathlib import Path

from importers import ImportErrorDetail, import_data


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TEMPLATE = ROOT / "materials/template.md"
TEMPLATE = PUBLIC_TEMPLATE if PUBLIC_TEMPLATE.exists() else ROOT.parent / "X原创检测提交表模板.md"


class DistributedTemplateTests(unittest.TestCase):
    def test_filled_current_template_preserves_only_supplied_source_body(self):
        template = TEMPLATE.read_text(encoding="utf-8-sig")
        body = "我比较了两种策略在三组订单中的表现，并统计缺货次数；这项观察还需要真实数据验证。"
        source = "准确预测可以改善需求计划。\n企业应监控预测值与实际销量之间的差异。"
        filled = template.replace("- 帖子链接：", "- 帖子链接：https://x.com/demo/status/123456789", 1)
        filled = filled.replace("- 对应帖子链接或编号：", "- 对应帖子链接或编号：P001", 1)
        filled = filled.replace("本人制作／他人来源／混合素材／不确定", "他人来源", 1)
        filled = filled.replace("- 创作方式：本人创作／工具辅助／自动生成或自动发布／其他／不确定", "- 创作方式：工具辅助", 1)
        filled = re.sub(r"(?m)^在这里粘贴完整原文[^\n]*$", lambda _: body, filled, count=1)
        filled = re.sub(r"(?m)^在这里粘贴需要对比的来源原文[^\n]*$", lambda _: source, filled, count=1)
        project = import_data(filled, "提交表.md")["project"]
        self.assertEqual(1, len(project["posts"]))
        post = project["posts"][0]
        self.assertEqual(body, post["text"])
        self.assertEqual(source, post["sources"][0]["text"])
        self.assertEqual("工具辅助", post["creation_method"])
        self.assertNotIn("有多个来源时", post["sources"][0]["text"])

    def test_blank_distributed_template_is_not_a_valid_post(self):
        template = TEMPLATE.read_bytes()
        with self.assertRaises(ImportErrorDetail):
            import_data(template, "提交表.md")

    def test_demo_selection_matches_gui_supported_value(self):
        sample = json.loads((ROOT / "sample-data.json").read_text(encoding="utf-8"))
        self.assertEqual("partial", sample["scope"]["selection"])
        self.assertIn("虚构", sample["account"])


if __name__ == "__main__":
    unittest.main()
