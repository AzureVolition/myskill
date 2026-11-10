# myskill

自用 Codex Skills 集合。

## 内容

- [`skills/translate-book`](skills/translate-book/)：图书翻译流水线，支持 PDF、DOCX、EPUB 输入，输出 Markdown、HTML、DOCX、EPUB 和带可点击目录的 PDF。

## 安装

将需要的 Skill 复制到 Codex Skills 目录：

```bash
cp -R skills/translate-book ~/.codex/skills/
python3 -m pip install --user "pypdf>=5,<7" pandoc
```

如果系统中没有 Calibre，还需要安装 `ebook-convert`。

## 使用

```text
使用 $translate-book 把这本书翻译成简体中文，生成 PDF、EPUB、DOCX 和 HTML。
```

PDF 生成后可自动包含章节书签和目录页跳转链接。
