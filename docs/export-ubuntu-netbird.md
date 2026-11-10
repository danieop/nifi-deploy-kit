# Export trực tiếp trên Ubuntu đã kết nối NetBird

Chạy `export.sh` trên máy cá nhân Ubuntu để đọc một NiFi/Docker Compose nguồn
qua mạng NetBird. Kết quả lưu tại local; không cần GitHub Actions, runner hoặc
GitHub Secrets. Máy cá nhân không cần cài Docker khi dùng `--docker-host`.

```text
Ubuntu cá nhân → NetBird → nguồn: SSH đọc Docker/file + GET API NiFi
       ↓
local/exports/customer-a-prod/{flow.json,config.json,assets/}
```

Nguồn phải thuộc [baseline được hỗ trợ](../README.md#scope): NiFi/Docker Compose
với Nginx Basic Auth, Python 3 và quyền đọc Docker/file hoặc sudo. Export không
restart/exec container hay sửa cấu hình nguồn; SSH/sudo/API có thể tạo audit log.
NetBird cung cấp đường mạng; vẫn cần credential SSH và API NiFi của nguồn.

## 1. Chuẩn bị trên Ubuntu cá nhân

NetBird đã đăng nhập và kết nối. Nếu thiếu công cụ:

```bash
sudo apt update
sudo apt install -y git python3 openssh-client iproute2 netcat-openbsd
```

Các lệnh dưới dùng Bash trên Ubuntu. Cần Python 3.10 trở lên; không có dependency pip.

```bash
python3 --version
netbird status

git clone https://github.com/danieop/nifi-deploy-kit.git
cd nifi-deploy-kit
mkdir -p local
chmod 700 local
umask 077
```

Nếu đã có repo, mở terminal tại thư mục repo đang dùng. Chạy script bằng user
thường trên máy cá nhân; quyền sudo cho Docker nguồn được xử lý riêng.

## 2. Điền thông tin nguồn

Ví dụ dưới đây dùng nguồn .235. Thay cả block theo server cần export; URL API và
SSH host phải thuộc cùng deployment. Không nhập password vào biến trong block này.

```bash
SOURCE_IP=10.0.0.235
SOURCE_USER=sangnguyen
SSH_PORT=22
NIFI_URL=http://10.0.0.235:9080/nifi
SOURCE_COMPOSE=/home/sangnguyen/nifi/compose.yml
SOURCE_PROJECT=nifi
BUNDLE_NAME=customer-a-prod
OUTPUT_DIR="local/exports/$BUNDLE_NAME"
```

- `SOURCE_COMPOSE`: đường dẫn tuyệt đối trên nguồn, không phải trên Ubuntu cá nhân.
- `SOURCE_PROJECT`: tên Compose project đang chạy. Có thể bỏ `--compose-project`
  ở lệnh export nếu muốn dùng tên Compose resolve được.
- Tên bundle dùng chữ thường, số và dấu gạch ngang, tối đa 63 ký tự.

## 3. Kiểm tra NetBird và đường tới nguồn

```bash
netbird status
ip route get "$SOURCE_IP"
nc -vz -w 5 "$SOURCE_IP" "$SSH_PORT"
nc -vz -w 5 "$SOURCE_IP" 9080
```

Đổi `9080` theo port thật trong NIFI_URL; nếu API dùng hostname/IP khác, kiểm tra
cả route và TCP tới địa chỉ đó. Với mạng đang kiểm chứng, route đi qua `wt0` và
table `netbird`; tên interface/table có thể khác theo cấu hình NetBird.

Trạng thái NetBird Connected chỉ xác nhận client kết nối, chưa chứng minh có
route/quyền tới nguồn. Cả SSH và API phải truy cập được. Nếu thiếu route hoặc bị
policy/firewall chặn, nhờ quản trị mạng cấp quyền trước khi export. Không cần
mở port nguồn ra Internet.

## 4. Xác minh SSH host key

Nếu có file known_hosts đã được quản trị viên xác minh, lưu thành
`local/known_hosts`. Nếu cần lấy candidate từ mạng:

```bash
ssh-keyscan -T 5 -p "$SSH_PORT" "$SOURCE_IP" > local/known_hosts.candidate
ssh-keygen -lf local/known_hosts.candidate
```

Đối chiếu fingerprint với quản trị viên qua kênh tin cậy. `ssh-keyscan` một mình
không xác minh danh tính server. **Sau khi khớp fingerprint**, dùng candidate:

```bash
mv local/known_hosts.candidate local/known_hosts
chmod 600 local/known_hosts
```

Với port khác 22, entry phải có dạng `[hostname-or-IP]:port`; ssh-keyscan đã tạo
đúng dạng khi dùng `-p`. Host/IP trong file phải khớp địa chỉ truyền cho SSH.

Kiểm tra đăng nhập và công cụ nguồn bằng lệnh chỉ đọc:

```bash
ssh -p "$SSH_PORT" \
  -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=local/known_hosts \
  "$SOURCE_USER@$SOURCE_IP" \
  'python3 --version && docker compose version'
```

Docker Compose version chưa xác nhận quyền đọc Docker daemon/file mounts. Lệnh
export sẽ kiểm tra các quyền đó khi inspect nguồn.

## 5. Chuẩn bị credential local

### API NiFi

Định dạng file cho `--auth-file` có hai key `username` và `password`:

```bash
cat > local/source-api.json <<'JSON'
{
  "username": "nifiadmin",
  "password": "REPLACE_WITH_SOURCE_API_PASSWORD"
}
JSON
chmod 600 local/source-api.json
nano local/source-api.json
```

Điền mật khẩu thật bằng editor, không ghi nó trực tiếp trong command/shell
history. Đây là tài khoản Basic Auth vào URL API nguồn. Với API anonymous có thể
bỏ `--auth-file`. Với API HTTPS dùng CA riêng, thêm
`--ca-file local/source-ca.pem`; không tắt TLS verification.

### SSH: chọn password hoặc key

**Dùng password**, nhập ẩn trên terminal rồi export biến cho script:

```bash
read -r -s -p 'SSH password: ' NIFI_SOURCE_SSH_PASSWORD
printf '\n'
export NIFI_SOURCE_SSH_PASSWORD
```

**Dùng key/SSH agent**, không cần nhập password SSH ở trên. Bỏ biến password
nếu đang có trong session; thêm `--ssh-identity "$HOME/.ssh/id_ed25519"` vào lệnh
export khi cần chọn key cụ thể. Key có passphrase nên được mở bằng SSH agent
trước để export chạy non-interactive.

```bash
# Chỉ chạy block này khi chọn key/SSH agent.
unset NIFI_SOURCE_SSH_PASSWORD
```

Nếu nguồn yêu cầu sudo để đọc Docker, dùng `--docker-sudo`. Script dùng
`NIFI_SOURCE_SUDO_PASSWORD` nếu có, rồi fallback SSH password; khi không có
password thì dùng sudo passwordless. Nếu sudo password khác SSH password, hoặc
SSH dùng key nhưng sudo cần password, nhập riêng:

```bash
read -r -s -p 'Source sudo password: ' NIFI_SOURCE_SUDO_PASSWORD
printf '\n'
export NIFI_SOURCE_SUDO_PASSWORD
```

Không đặt các password này trong lệnh `--...`, file tracked hay shell history.

## 6. Chạy export

Ví dụ nguồn cần sudo, service names là `nifi`/`nginx`:

```bash
./export.sh \
  --name "$BUNDLE_NAME" \
  --url "$NIFI_URL" \
  --auth-file local/source-api.json \
  --docker-host "$SOURCE_USER@$SOURCE_IP" \
  --ssh-port "$SSH_PORT" \
  --ssh-known-hosts local/known_hosts \
  --compose-file "$SOURCE_COMPOSE" \
  --compose-project "$SOURCE_PROJECT" \
  --docker-sudo \
  --output "$OUTPUT_DIR"
```

Bỏ `--docker-sudo` nếu tài khoản đọc được Docker/file không cần sudo. Nếu service
names khác, thêm `--nifi-service TEN_SERVICE` và `--gateway-service TEN_GATEWAY`.
Mặc định export root; thêm `--group-id ID` nếu chỉ export một process group.

Lệnh đọc Docker Compose/container settings và các bind files qua SSH, đọc flow,
controller services và parameter contexts qua GET API. Nó không cài phần mềm
hoặc ghi cấu hình lên nguồn. `--docker-local` dành cho trường hợp chạy ngay tại
Docker host nguồn, không dùng cho đường SSH qua NetBird ở hướng dẫn này.

Sau khi xong (kể cả khi export lỗi), bỏ password khỏi environment của terminal:

```bash
unset NIFI_SOURCE_SSH_PASSWORD NIFI_SOURCE_SUDO_PASSWORD
```

## 7. Kiểm tra và bàn giao bundle

```bash
python3 -m nifi_deploy validate "$OUTPUT_DIR"
ls -la "$OUTPUT_DIR"
```

Kết quả:

```text
local/exports/customer-a-prod/
├── flow.json
├── config.json
└── assets/
```

Native sensitive values được thay bằng placeholder hoặc bỏ khỏi flow; admin
cấp secret máy đích trong config.json. Dữ liệu script/URL/text/binary thông thường
không được bảo đảm scrub mọi thông tin nội bộ. File API auth, key và known_hosts
của máy cá nhân không được đưa vào bundle. Thư mục local đã được gitignore.

Chuyển **toàn bộ folder bundle** sang máy đích. Máy đích cần code NiFi Deploy Kit;
admin chỉnh config.json và chạy `./deploy.sh --config DUONG_DAN/config.json` theo
[hướng dẫn deploy](admin-vi.md). Không cần chuyển credential truy cập nguồn.

Export trực tiếp không tự upload GitHub, tạo artifact hay cập nhật main. Nếu cần
lưu bundle lên repo, tạo branch từ main và chạy export với
`--output "bundles/$BUNDLE_NAME"`, kiểm tra nội dung được phép công khai rồi
commit/push và mở PR. Giữ file credential trong local, không stage cả thư mục đó.

## 8. Xử lý lỗi

| Biểu hiện | Kiểm tra |
|---|---|
| NetBird Connected nhưng TCP timeout | Route tới IP thật, policy NetBird, routing peer và firewall/port nguồn |
| SSH host key verification failed | Entry đúng IP/hostname/port và fingerprint đã xác minh; không tắt StrictHostKeyChecking |
| SSH Permission denied | SSH username, password/key hoặc SSH agent; credential API không thay thế credential SSH |
| Docker source capture failed | SSH trust/auth, Compose file/project, Python/Docker Compose, quyền daemon và bind files; thêm sudo nếu cần |
| API HTTP 401/403 | URL/API username/password và quyền đọc nguồn |
| API connection unavailable | Route/TCP tới API, port, HTTPS CA; client Python truy cập trực tiếp, không dùng HTTP_PROXY/HTTPS_PROXY |
| Model không được hỗ trợ | Đối chiếu baseline NiFi/Nginx/Compose, mounts và giới hạn assets trong README |
| Validate checksum lỗi | Giữ nguyên bundle đầy đủ; export lại vào output riêng nếu file bị thiếu/sửa |

Tham khảo NetBird: [CLI](https://docs.netbird.io/get-started/cli) và
[kiểm tra kết nối](https://docs.netbird.io/help/troubleshooting-client).
