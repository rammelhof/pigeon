---
name: container-tools
description: Use when choosing installed command-line tools in this Docker environment.
---

# Container tools

Preinstalled in this container (Debian bookworm, `node:24` base):

- `gh` — GitHub CLI; authenticated as configured in this container.
- `pandoc` — convert Markdown, HTML, and document formats.
- `jq`, `yq` — query and transform JSON / YAML.
- `rg` — fast text search (ripgrep); `file`, `tree`, `less` for inspecting.
- `curl`, `wget`, `http` (httpie), `axel` — downloads and HTTP requests.
- `zip`, `unzip`, `7z`, `unrar-free`, `rsync`, `xz` — archives and transfer.
- `soffice` (LibreOffice) — convert/inspect docx, xlsx, pptx; `magick`
  (ImageMagick) for images; `ffmpeg` for audio/video.
- `pdftoppm`, `pdfinfo` (poppler), `qpdf`, `gs` (Ghostscript),
  `tesseract` (OCR, eng+deu) — PDF work.
- `sqlite3`, `redis-cli`, `xmlstarlet`, `dos2unix`, `hunspell`
  (`hunspell -d de_AT` / `-d en_US`).
- `python3`, `node` — scripting; `npm` for Node tooling.
- `dotnet` — .NET SDK 10 (C#/F#): `dotnet new console|web`, `dotnet build`,
  `dotnet run`, `dotnet test`.
- `dig`, `nc`, `lsof`, `htop`, `tmux`, `ssh` (OpenSSH client) — network
  and process inspection.
- `bwrap` — bubblewrap (pigeon uses it to sandbox pi runs).

Before using a tool, check `command -v <tool>` and, when useful,
`<tool> --help`.
Do not assume credentials, network access, or a particular tool version.
