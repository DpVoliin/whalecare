#!/bin/bash
# 生成发布签名密钥，并写好 collector/keystore.properties
# 用法：bash collector/tools/make_release_keystore.sh
# 注意：这个密钥一旦用于发布就**不能丢**（丢了就无法给已发布的包做升级），请自己备份好。
set -eu
cd "$(dirname "$0")/.."          # → collector/
mkdir -p keystore
KS="keystore/release.jks"
if [ -f "$KS" ]; then
  echo "  $KS 已存在，不覆盖（要重建请先自己删掉）"; exit 0
fi
read -r -p "  组织/名字（如 DpVoliin）: " CN
read -r -p "  密码（至少 6 位）: " PW
keytool -genkeypair -v \
  -keystore "$KS" -alias whale -keyalg RSA -keysize 4096 -validity 10950 \
  -storepass "$PW" -keypass "$PW" \
  -dname "CN=$CN, OU=whalecare, O=whalecare, L=Guangzhou, ST=Guangdong, C=CN"
cat > keystore.properties <<EOF
storeFile=keystore/release.jks
storePassword=$PW
keyAlias=whale
keyPassword=$PW
EOF
chmod 600 keystore.properties "$KS"
echo "  ✓ 已生成 $KS 和 keystore.properties（已 chmod 600）"
echo "  ✓ 备份提醒：$KS 丢失后无法给已发布版本做升级"
echo "  下一步：cd collector && /opt/gradle/bin/gradle :app:assembleRelease"
