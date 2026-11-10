# Export server bất kỳ bằng GitHub Actions

Một lần chạy = một nguồn = một bundle. Không cần thêm server vào source list,
không sửa workflow và không đặt tên Secret theo IP. Workflow hiện tại chỉ chạy
code trên default branch `main`, trên trusted self-hosted Linux/x64 runner.

## 1. Chuẩn bị một lần cho mỗi nguồn

Runner phải truy cập được URL API và SSH của server nguồn. Source phải là mô hình
được hỗ trợ: Docker Compose, NiFi HTTP qua Nginx Basic Auth với mount `.htpasswd`.
Nguồn cần Python 3/Docker Compose và quyền đọc Docker/file (hoặc sudo). Việc chọn
server tùy ý không mở rộng thành hỗ trợ Kubernetes/bare metal hay mọi kiểu auth.

Tạo credential profile tại máy quản trị, ngoài Git:

```bash
mkdir -p local
chmod 700 local
cp examples/export-credentials.example.json local/customer-a-credentials.json
chmod 600 local/customer-a-credentials.json
nano local/customer-a-credentials.json
```

Ví dụ **chỉ minh họa**, thay bằng giá trị của nguồn:

```json
{
  "apiUsername": "nifiadmin",
  "apiPassword": "PASSWORD_API_NGUON",
  "sshPassword": "PASSWORD_SSH_NGUON",
  "knownHosts": "nifi.customer.lan ssh-ed25519 HOST_PUBLIC_KEY_DA_XAC_MINH"
}
```

| Key profile | Ý nghĩa |
|---|---|
| `apiUsername`, `apiPassword` | HTTP Basic Auth vào API nguồn; có thể bỏ cả hai với API anonymous |
| `sshPassword` | Password SSH, bỏ khi dùng private key/SSH agent trên runner |
| `sshPrivateKey` | Optional toàn bộ nội dung key, dùng JSON `\n` để biểu diễn xuống dòng; key phải dùng được non-interactive |
| `sudoPassword` | Optional password sudo riêng; bỏ để dùng SSH password fallback hoặc sudo passwordless |
| `knownHosts` | **Bắt buộc**: nội dung known_hosts đã xác minh; chứa host/IP khớp `docker_host` |
| `apiCaPem` | Optional CA PEM cho API HTTPS nguồn dùng CA riêng; không tắt TLS verification |

Với SSH port khác 22, known_hosts dùng dạng `[hostname]:port`. Nhiều dòng dùng
`\n` trong chuỗi JSON. Xác minh fingerprint qua kênh tin cậy; `ssh-keyscan` một
mình không chứng minh danh tính host. Không tự chấp nhận host key mới trong CI.

Đưa profile vào GitHub Actions Secret có tên do bạn chọn:

```bash
gh secret set CUSTOMER_A_EXPORT --repo danieop/nifi-deploy-kit \
  < local/customer-a-credentials.json
```

Hoặc Settings → Secrets and variables → Actions → New repository secret. Chỉ
nhập **tên Secret** vào form workflow; không nhập JSON/password/private key vào
workflow inputs vì những input đó không phải secret fields.

Mỗi nguồn có thể dùng profile khác nhau: `CUSTOMER_A_EXPORT`, `CUSTOMER_B_EXPORT`…
Đổi password/key chỉ cập nhật Secret đó. Profile chỉ dùng để truy cập nguồn;
credential cho máy đích vẫn được admin điền trong config.json sau khi tải bundle.

## 2. Chạy từ giao diện GitHub

Actions → **Export NiFi** → **Run workflow**, branch **main**.

| Input | Mặc định / quy tắc |
|---|---|
| `bundle_name` | Bắt buộc; tên bất kỳ 1–63 ký tự chữ thường/số/gạch ngang, ví dụ `customer-a-prod` |
| `nifi_url` | Bắt buộc; URL HTTP(S) nguồn, thường kết thúc `/nifi` hoặc `/nifi-api`; không nhúng credential vào URL |
| `docker_host` | Bắt buộc; `user@hostname` hoặc `user@IPv4` |
| `compose_file` | Bắt buộc; đường dẫn tuyệt đối trên nguồn |
| `credentials_secret` | Tên Secret profile; default `NIFI_EXPORT_CREDENTIALS` nếu bạn tạo profile chung với tên này |
| `compose_project` | Blank: dùng tên project Compose resolve được; điền nếu nguồn dùng `docker compose -p ...` |
| `ssh_port` | `22` |
| `docker_sudo` | `false`; bật nếu cần sudo để đọc Docker |
| `nifi_service` | `nifi`; đổi theo service name nguồn |
| `gateway_service` | `nginx`; đổi theo service name nguồn |
| `group_id` | Blank: export root; có thể chọn process group con |
| `runner_label` | `nifi-network`; label của trusted self-hosted Linux/x64 runner tới được mạng nguồn |
| `publish` | `false`; bật để đẩy thêm nhánh cập nhật repo cho maintainer review |

Tên bundle độc lập với hostname/IP. Dùng lại cùng tên để cập nhật cùng bundle;
đặt tên mới nếu muốn giữ thành bundle riêng. Giữ credential trong profile riêng
khi thay nguồn. Không dùng dấu gạch chéo/`..` trong bundle_name.

Với nhiều mạng khách hàng, gắn label riêng cho runner từng mạng và chọn label
đúng. Các lần export được serialize để tránh đồng thời ghi bundle. Workflow sẽ
chờ nếu không có runner phù hợp. Workflow không đăng ký/cài runner trên nguồn.

## 3. Chạy bằng GitHub CLI

```bash
gh workflow run export.yml --repo danieop/nifi-deploy-kit --ref main \
  -f bundle_name=customer-a-prod \
  -f nifi_url=http://nifi.customer.lan:9080/nifi \
  -f docker_host=ubuntu@nifi.customer.lan \
  -f compose_file=/opt/nifi/compose.yml \
  -f credentials_secret=CUSTOMER_A_EXPORT \
  -f compose_project=nifi \
  -f docker_sudo=true \
  -f publish=true

gh run list --repo danieop/nifi-deploy-kit --workflow export.yml --limit 5
```

Để export server khác, thay input host/URL/Compose, bundle_name và tên Secret.
Không sửa code, không sửa hosts.json và không cần tên chứa node/số IP.

## 4. Kết quả và tải về

Mỗi lần thành công upload artifact **`nifi-export-RUN_ID`** (retention 14 ngày).
Workflow mới chỉ upload folder `bundles/<bundle_name>/`, nên bên trong artifact:

```text
flow.json
config.json
assets/
```

Đăng nhập gh, thay RUN_ID bằng run vừa chọn, dùng thư mục riêng mỗi lần tải:

```bash
gh run download RUN_ID --repo danieop/nifi-deploy-kit \
  -n nifi-export-RUN_ID -D local/export-RUN_ID
nano local/export-RUN_ID/config.json
./deploy.sh --config local/export-RUN_ID/config.json
```

Artifact của workflow v0.2 cũ chứa nhiều bundle trong các thư mục node235/node237;
đừng nhầm đường dẫn với artifact root của workflow mới. Format flow/config vẫn
schema 2; deploy interface giữ nguyên `--config`.

Nếu bật publish và bundle khác Git, CI tạo nhánh
`Feat/refresh-nifi-export-<bundle_name>-<run_id>`, chỉ stage bundle đó. Mở PR theo
compare URL trong log và merge sau review. Main, release tags, config local đã
sửa và deployment máy khách không tự cập nhật. Không có thay đổi thì chỉ tạo
artifact, không tạo nhánh thừa. Artifact có trước bước push; nếu push lỗi, đọc
kết quả từng step, không coi run lỗi toàn bộ là đã publish thành công.

Repo này public: người dùng GitHub có read access có thể tải artifact; tắt publish
chỉ tắt nhánh Git, **không làm artifact thành private**. Native secrets được mask,
nhưng script/URL/ordinary text/binary có thể chứa thông tin nội bộ; chỉ export nội
dung được phép chia sẻ lên repo này, hoặc dùng một repo private phù hợp.

## 5. Ghi chú nâng từ v0.2

Không còn đọc `sources/hosts.json`, `sources/known_hosts`, variables SOURCE_235_URL /
SOURCE_237_URL hoặc các Secret NIFI_SOURCE_* của workflow cũ. Những Secret cũ
không bị tự xóa; có thể giữ cho công cụ khác, nhưng không điều khiển workflow mới.
Hai bundle node235/node237 có sẵn chỉ là dữ liệu mẫu đã export. Credential profile
mới không được đưa vào artifact/bundle. Temp files bị xóa cả khi export lỗi; các
field giải mã được đăng ký mask trong log trước khi sử dụng.
