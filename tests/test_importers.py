import io
import json
import unittest
import zipfile

from importers import ImportErrorDetail, import_data, normalize_project


class ImportersTests(unittest.TestCase):
    def test_saved_project_roundtrip_retains_scope_sources_and_media(self):
        raw = {"account": "@sample", "scope": {"start": "2026-07-09", "end": "2026-10-06", "timezone": "UTC+8", "complete": False},
               "posts": [{"id": "one", "text": "完整帖子", "text_complete": None, "sources": [{"text": "来源正文", "url": "https://example.test", "owner": "third_party"}],
                          "media": [{"name": "one.png", "hash": "a" * 64, "kind": "image"}], "contribution": "我的贡献"}],
               "selected_candidates": ["one"]}
        normalized = normalize_project(raw)
        normalized_again = import_data(json.dumps(normalized, ensure_ascii=False), "project.json")["project"]
        self.assertEqual(normalized, normalized_again)
        self.assertIsNone(normalized["posts"][0]["text_complete"])
        self.assertEqual("other", normalized["posts"][0]["sources"][0]["owner"])

    def test_json_wrappers_and_archive_tweet_relationship(self):
        record = {"tweet": {"id_str": "123456789", "full_text": "原帖正文", "created_at": "Tue Oct 06 00:00:00 +0000 2026", "in_reply_to_status_id_str": "111"}}
        for key in ("posts", "records", "tweets"):
            p = import_data(json.dumps({key: [record]}), "posts.json")["project"]["posts"][0]
            self.assertEqual("reply", p["type"])
            self.assertEqual("111", p["reply_to"])
            self.assertEqual("https://x.com/i/status/123456789", p["url"])

    def test_archive_js_never_executes_trailing_code_or_dm_assignment(self):
        text = 'window.YTD.tweets.part0 = [{"tweet":{"id_str":"123456789","full_text":"公开帖"}}];'
        self.assertEqual(1, len(import_data(text, "tweets.js")["project"]["posts"]))
        for unsafe in (text + " alert('run')", "window.YTD.direct_messages.part0 = [];", "(()=>{return []})()", "window.YTD.tweets.part0 = [undefined]"):
            with self.assertRaises(ImportErrorDetail):
                import_data(unsafe, "tweets.js")

    def test_csv_bom_multiline_and_chinese_headers(self):
        csv_data = '编号,帖子链接,正文,类型,来源原文,素材归属\r\nP001,https://x.com/me/status/1,"第一行\n第二行",引用帖,来源内容,他人来源\r\n'
        post = import_data(b"\xef\xbb\xbf" + csv_data.encode(), "帖子.csv")["project"]["posts"][0]
        self.assertEqual("第一行\n第二行", post["text"])
        self.assertEqual("quote", post["type"])
        self.assertEqual("other", post["sources"][0]["owner"])

    def test_csv_broken_row_is_visible_error(self):
        with self.assertRaises(ImportErrorDetail):
            import_data("id,text\n1,abc,unexpected", "posts.csv")
        with self.assertRaises(ImportErrorDetail):
            import_data('id,text\n1,"unclosed', "posts.csv")

    def test_partial_invalid_json_reports_not_silent(self):
        result = import_data('[{"id":"1","text":"abc"},null,42]', "posts.json")
        self.assertEqual(1, len(result["project"]["posts"]))
        self.assertTrue(any("2 条记录" in w for w in result["warnings"]))
        with self.assertRaises(ImportErrorDetail):
            import_data("[null,42]", "posts.json")

    def test_same_record_not_counted_twice_conflict_retained(self):
        value = [{"id": "P1", "text": "第一条"}, {"id": "P1", "text": "第一条"}, {"id": "P1", "text": "冲突内容"}]
        result = import_data(json.dumps(value), "posts.json")
        self.assertEqual(2, len(result["project"]["posts"]))
        self.assertEqual(2, len(result["warnings"]))
        self.assertNotEqual(*[p["id"] for p in result["project"]["posts"]])

    def test_zip_ignores_account_dm_and_hashes_only_referenced_media(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("data/tweets.js", 'window.YTD.tweets.part0 = [{"tweet":{"id_str":"123456789","full_text":"公开正文","entities":{"media":[{"media_url_https":"https://pbs.example/x.jpg","type":"photo"}]}}}];')
            archive.writestr("data/direct-messages.js", b"this file must never be parsed")
            archive.writestr("data/account.js", b"this file must never be parsed")
            archive.writestr("data/tweets_media/123456789-x.jpg", b"local-media")
            archive.writestr("data/tweets_media/unrelated.jpg", b"not-for-this-post")
        result = import_data(stream.getvalue(), "archive.zip")
        self.assertEqual(["data/tweets.js"], result["imported_files"])
        post = result["project"]["posts"][0]
        self.assertEqual(64, len(post["media"][0]["hash"]))
        self.assertEqual("local_file", post["media"][0]["hash_origin"])
        self.assertTrue(any("未读取私信" in w for w in result["warnings"]))

    def test_zip_traversal_and_unknown_files_rejected(self):
        for filename in ("../posts.json", "data/account.js"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr(filename, "[]")
            with self.assertRaises(ImportErrorDetail):
                import_data(stream.getvalue(), "archive.zip")

    def test_markdown_template_parses_sources_and_thread(self):
        content = '''# 提交表
## 一、基本信息
- 账号链接或 @用户名：@demo
- 时间范围（账号评估必填；仅样本初评可填“不适用”）：2026-07-09 至 2026-10-06
- 时区：UTC+8
- 是否包含该期间全部本人发帖：否
- 已知该期间帖子总数：25
## 二、申请候选
1. 帖子链接：https://x.com/demo/status/123456789
## 三、帖子材料
#### P001
- 帖子链接：https://x.com/demo/status/123456789
- 发布时间（含时区；不知道则填未知）：2026-10-06T09:00:00+08:00
- 类型：主帖
- 所属线程及顺序：实验记录，第 1 条，共 3 条
- 正文或长文是否完整：是
**帖子原文：**
实际正文，保留两行。
第二行。
**对应媒体：**
- 图片文件：P001_图1.jpg
- 无法提供的媒体及原因：视频丢失
**本人补充说明（可选）：**
背景说明。
## 四、来源与原创贡献
### 来源记录 S001
- 对应帖子链接或编号：P001
- 素材归属：他人来源
- 来源链接或出处：https://source.test/post
- 本人新增的分析、观点、报道或创作：比较和实测
- 创作方式：工具辅助
- 证据文件名：draft.md
## 五、收益申请资格信息
- 当前资格状态：未知
'''
        project = import_data(content, "提交表.md")["project"]
        post = project["posts"][0]
        self.assertEqual("@demo", project["account"])
        self.assertEqual("2026-07-09", project["scope"]["start"])
        self.assertEqual(3, post["thread_total"])
        self.assertEqual("实际正文，保留两行。\n第二行。", post["text"])
        self.assertEqual("P001_图1.jpg", post["media"][0]["name"])
        self.assertEqual("https://source.test/post", post["sources"][0]["url"])
        self.assertEqual("比较和实测", post["contribution"])
        self.assertEqual(["draft.md"], post["evidence"])

    def test_empty_template_is_rejected(self):
        with self.assertRaises(ImportErrorDetail):
            import_data("#### P001\n- 帖子链接：\n**帖子原文：**\n在这里粘贴完整原文。\n**对应媒体：**\n- 图片文件：", "提交表.md")

    def test_limits_and_bad_hash(self):
        with self.assertRaises(ImportErrorDetail):
            import_data(b"x" * (30 * 1024 * 1024 + 1), "posts.json")
        project = normalize_project({"posts": [{"text": "abc", "media": [{"name": "x.png", "hash": "pretend"}]}]})
        self.assertEqual("", project["posts"][0]["media"][0]["hash"])

    def test_candidates_filtered_and_known_missing_array_supported(self):
        raw = {"scope": {"known_missing": ["图片", "线程分段"]},
               "posts": [{"id": "P001", "url": "https://x.com/demo/status/1", "text": "正文"}],
               "selected_candidates": ["P001", "https://x.com/demo/status/1", {"id": "P001"}, 12, "missing"]}
        normalized = normalize_project(raw)
        self.assertEqual(["P001"], normalized["selected_candidates"])
        self.assertEqual("图片；线程分段", normalized["scope"]["known_missing"])

    def test_post_count_limit(self):
        with self.assertRaises(ImportErrorDetail):
            normalize_project({"posts": [{"id": str(i), "text": "正文"} for i in range(2001)]})

    def test_markdown_source_multiline_and_self_first_publish_fallback(self):
        raw = '''#### P001
- 帖子链接：https://x.com/demo/status/1
**帖子原文：**
实际正文。
**对应媒体：**
## 四、来源与原创贡献
### 来源记录 S001
- 对应帖子链接或编号：P001
- 素材归属：本人制作
- 来源链接或出处：
- 本人在其他平台发布的链接（适用时填写）：https://example.org/my-post
**来源原文（用于文字对比，可选）：**
第一行来源。
第二行来源。
## 五、收益申请资格信息
'''
        source = import_data(raw, "提交表.md")["project"]["posts"][0]["sources"][0]
        self.assertEqual("https://example.org/my-post", source["url"])
        self.assertEqual("第一行来源。\n第二行来源。", source["text"])
        self.assertEqual("self", source["owner"])

    def test_zip_private_media_folder_never_used_as_post_media(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("posts.json", json.dumps({"posts": [{"id": "a", "text": "正文", "media": [{"name": "secret.jpg"}]}]}))
            archive.writestr("data/direct-messages_media/secret.jpg", b"private-media")
        post = import_data(stream.getvalue(), "archive.zip")["project"]["posts"][0]
        self.assertEqual("", post["media"][0]["hash"])


if __name__ == "__main__":
    unittest.main()
