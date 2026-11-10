# Export NiFi từ máy cá nhân Ubuntu

Giả định Ubuntu đã kết nối được server qua NetBird, có tài khoản NiFi và tài
khoản SSH dùng IP, username, password. Các lệnh dưới chạy bằng Bash.

## 1. Mở repo và điền thông tin nguồn

Nếu chưa có repo:

```bash
git clone https://github.com/danieop/nifi-deploy-kit.git
cd nifi-deploy-kit
```

Mở Terminal tại thư mục `nifi-deploy-kit`. Sửa giá trị trong đoạn lệnh dưới
rồi **dán vào Terminal**; đây là biến của terminal, không phải file cấu hình.
Giữ cùng terminal để chạy các bước tiếp theo:

```bash
SOURCE_IP=10.0.0.235
SSH_USER=sangnguyen
NIFI_URL=http://10.0.0.235:9080/nifi
COMPOSE_FILE=/home/sangnguyen/nifi/compose.yml
BUNDLE_NAME=customer-a-prod

umask 077
mkdir -p local
```

`COMPOSE_FILE` là đường dẫn file Compose **trên server nguồn**.

Nếu đây là lần đầu SSH từ máy cá nhân tới nguồn, chạy
`ssh "$SSH_USER@$SOURCE_IP" exit` một lần để xác nhận fingerprint của server.

## 2. Nhập tài khoản và mật khẩu

```bash
read -r -p 'NiFi username: ' NIFI_SOURCE_USERNAME
read -r -s -p 'NiFi password: ' NIFI_SOURCE_PASSWORD
printf '\n'
read -r -s -p 'SSH password: ' NIFI_SOURCE_SSH_PASSWORD
printf '\n'
export NIFI_SOURCE_USERNAME NIFI_SOURCE_PASSWORD NIFI_SOURCE_SSH_PASSWORD
```

Password được nhập ẩn, không cần tạo file credential.

## 3. Chạy export

```bash
./export.sh \
  --name "$BUNDLE_NAME" \
  --url "$NIFI_URL" \
  --docker-host "$SSH_USER@$SOURCE_IP" \
  --compose-file "$COMPOSE_FILE" \
  --docker-sudo \
  --output "local/exports/$BUNDLE_NAME"
```

`--docker-sudo` dùng password SSH cho sudo trên nguồn. Bỏ flag này nếu tài khoản
đã có quyền đọc Docker không cần sudo.

Sau khi lệnh kết thúc, bỏ credential khỏi terminal:

```bash
unset NIFI_SOURCE_USERNAME NIFI_SOURCE_PASSWORD NIFI_SOURCE_SSH_PASSWORD
```

## 4. Kiểm tra kết quả

```bash
python3 -m nifi_deploy validate "local/exports/$BUNDLE_NAME"
```

Bundle nằm tại:

```text
local/exports/customer-a-prod/
├── flow.json
├── config.json
└── assets/
```

Export chỉ đọc nguồn và lưu kết quả trên máy cá nhân. Không tự upload GitHub.
