SHELL := /bin/bash
.DEFAULT_GOAL := help
PY ?= python3
VERSION := $(shell awk -F= '/^version[[:space:]]*=/{gsub(/[[:space:]]/,"",$$2);print $$2}' manifest)

.PHONY: help version test lint check build verify clean preview

help: ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

version: ## 打印当前版本号
	@echo $(VERSION)

test: ## 运行全部单元测试（含仓库一致性检查）
	$(PY) -m pytest tests/ -q

lint: ## Shell / Python 语法检查
	@bash -n app/assets/oes-boot-sync.sh
	@bash -n tools/build.sh
	@for f in app/bin/oesboot cmd/*; do \
		if head -n1 "$$f" | grep -qE '^#!.*(ba)?sh'; then bash -n "$$f" || exit 1; fi; \
	done
	@$(PY) -c "import ast,glob; [ast.parse(open(f,encoding='utf-8').read()) for f in glob.glob('app/**/*.py',recursive=True)]"
	@echo "语法检查通过"

check: ## 版本号 / 必需文件 / 换行符 / 危险钩子检查
	@$(PY) tools/check_consistency.py

verify: lint check test ## 提交前的完整验证

build: ## 构建 fpk（需联网下载 fnpack，产物在 dist/）
	bash tools/build.sh

clean: ## 清理缓存与构建残留（保留 dist/）
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -name '*.pyc' -delete 2>/dev/null || true
	@rm -rf .pytest_cache app/app.sock oesboot.fpk
	@echo "已清理"

preview: ## 用假数据在本机预览界面（无需 OES 设备）
	@echo "打开 http://127.0.0.1:8765  （Ctrl-C 退出）"
	OESBOOT_FAKE=$(CURDIR)/dev/fake-status.json OESBOOT_PORT=8765 $(PY) app/server.py
