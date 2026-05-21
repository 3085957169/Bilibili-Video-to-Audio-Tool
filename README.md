[README.txt](https://github.com/user-attachments/files/28094679/README.txt)<img width="1275" height="711" alt="演示" src="https://github.com/user-attachments/assets/5dc6e46e-8463-4059-89d6-9d1edbfb9609" />
<img width="1275" height="711" alt="演示" src="https://github.com/user-attachments/assets/6b431648-1369-4bf9-a6e1-8037269eb566" />

========================================
    B站视频转音频工具 — 本机部署教程
========================================

【环境要求】
  - Python 3.8+
  - ffmpeg（音频转码）
  - yt-dlp（视频下载）

【安装步骤】
  1. 安装 Python 依赖:
     pip install yt-dlp requests

  2. 安装 ffmpeg:
     Windows: winget install Gyan.FFmpeg
             或 https://ffmpeg.org/download.html
     macOS:   brew install ffmpeg
     Linux:   sudo apt install ffmpeg

【启动服务】
  在解压目录打开终端，运行:
    python serve.py

  看到 "Server ready: http://0.0.0.0:8888" 即成功。

【使用】
  浏览器打开 http://localhost:8888
  - 搜索框输入关键词 / BV号 / B站链接
  - 点 + 下载音频到本机
  - 点 ▶ 在线播放
  - 播放过的歌曲自动缓存，再次播放秒开

【修改端口】
  编辑 serve.py，改 PORT = 8888 为其他端口。
  外网访问需放行对应端口（防火墙/安全组）。

【设置下载路径】
  服务器默认保存在 serve.py 同目录。
  MCP 工具可调用 /api/settings 修改路径。
