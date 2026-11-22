#!/usr/bin/env python3
"""把 src.html（页面内容）包成可以直接用手机浏览器打开的 index.html。用法：python3 build.py"""
from pathlib import Path

here = Path(__file__).parent
body = (here / "src.html").read_text(encoding="utf-8")
head = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#FDF1F4">
<style>:root{padding:env(safe-area-inset-top,0px) 0 env(safe-area-inset-bottom,0px);box-sizing:border-box;height:100%}body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style>
</head>
<body>
"""
(here / "index.html").write_text(head + body + "</body>\n</html>\n", encoding="utf-8")
print("已生成", here / "index.html")
