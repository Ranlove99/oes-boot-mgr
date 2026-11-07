#!/bin/bash
# 构建 fpk：清理 → fnpack build → 校验结构/checksum → 重命名为 <appname>_<version>_<platform>.fpk
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
FNPACK="${FNPACK:-}"
VERSION="$(awk -F= '/^version[[:space:]]*=/{gsub(/[[:space:]]/,"",$2);print $2}' "$DIR/manifest")"
APPNAME="$(awk -F= '/^appname[[:space:]]*=/{gsub(/[[:space:]]/,"",$2);print $2}' "$DIR/manifest")"
PLATFORM="$(awk -F= '/^platform[[:space:]]*=/{gsub(/[[:space:]]/,"",$2);print $2}' "$DIR/manifest")"
OUT="$DIR/dist"

echo "==> $APPNAME $VERSION ($PLATFORM)"

# 1) 清理
find "$DIR" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$DIR" -name '*.pyc' -delete 2>/dev/null || true
find "$DIR" -name '.DS_Store' -delete 2>/dev/null || true
rm -f "$DIR"/app/app.sock "$DIR"/dist/*.fpk 2>/dev/null || true

# 2) fnpack
if [ -z "$FNPACK" ] || [ ! -x "$FNPACK" ]; then
  FNPACK="/tmp/fnpack-1.2.3-linux-amd64"
  if [ ! -x "$FNPACK" ]; then
    echo "==> 下载 fnpack 1.2.3"
    curl -fL -o "$FNPACK" https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64
    chmod +x "$FNPACK"
  fi
fi

echo "==> fnpack build"
(cd "$DIR" && "$FNPACK" build --directory "$DIR")

# 3) 产物归档
mkdir -p "$OUT"
SRC="$DIR/$APPNAME.fpk"
[ -f "$SRC" ] || { echo "构建产物不存在"; exit 1; }
TARGET="$OUT/${APPNAME}_${VERSION}_${PLATFORM}.fpk"
mv -f "$SRC" "$TARGET"

# 4) 校验
echo "==> 校验结构"
LIST="$(tar tzf "$TARGET")"
for f in app.tgz cmd/install_init cmd/install_callback cmd/main config/privilege config/resource manifest ICON.PNG ICON_256.PNG; do
  printf '%s\n' "$LIST" | grep -qx "$f" || { echo "缺少 $f"; exit 1; }
done
CK=$(tar xzOf "$TARGET" manifest | awk -F= '/^checksum/{gsub(/ /,"",$2);print $2}')
MD5=$(tar xzOf "$TARGET" app.tgz | md5sum | awk '{print $1}')
[ "$CK" = "$MD5" ] || { echo "checksum 不匹配: $CK != $MD5"; exit 1; }

echo "==> 完成: $TARGET"
ls -lh "$TARGET"
# 在 dist/ 内生成相对路径的校验文件，用户下载后可直接 sha256sum -c，
# 不会出现校验文件里写着构建机绝对路径的情况。
(cd "$OUT" && sha256sum "${APPNAME}_${VERSION}_${PLATFORM}.fpk" \
    | tee "${APPNAME}_${VERSION}.sha256")
