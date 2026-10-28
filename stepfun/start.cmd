@echo off
rem 一键启动：起 seekdb 容器 + 启动知识库服务
setlocal

set NODE=C:\Users\mrx21\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe
set DOCKER=C:\Program Files\Docker\Docker\resources\bin\docker.exe

echo [1/2] 启动 seekdb 容器 ...
"%DOCKER%" start seekdb >nul 2>&1
"%DOCKER%" ps --filter name=seekdb --format "{{.Names}} {{.Status}}"

echo [2/2] 启动应用 ...
cd /d "%~dp0server"
"%NODE%" index.js
