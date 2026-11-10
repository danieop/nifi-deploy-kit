# Export Docker + NiFi và triển khai lại trên một máy

Từ phiên bản 0.2, admin **chỉ sửa một `config.json`**. Không cần tạo `.env`,
`parameters.local.json` hay `components.local.json` cho bundle mới.

## 1. Kết quả export gồm gì?

```text
node235/
├── flow.json      # Native NiFi flow: processors, services, contexts, wiring
├── config.json    # Docker lấy từ nguồn + mọi giá trị admin cần chỉnh
└── assets/        # File mount, script khởi động, Nginx config, NAR/JAR
```

Các binary/script là dependency thật, không thể thay bằng JSON. Docker config
được đọc từ Compose đã resolve và container đang chạy; không dùng Compose mẫu
cố định của v0.1. Image được ghim theo digest khi Docker nguồn có RepoDigest.

Nguồn chỉ được inspect: API GET, SSH chạy Docker config/inspect và đọc file bind.
Không cài phần mềm, không ghi file, không restart/exec container trên VM nguồn.
SSH/sudo/API vẫn có thể tạo audit log bình thường. Export lúc không có người sửa
cấu hình nguồn để tránh hai snapshot lấy ở hai thời điểm khác nhau.

## 2. Admin quản lý nguồn và GitHub

1. Trong `sources/hosts.json`, khai báo URL NiFi, `dockerHost=user@host`, đường dẫn
   tuyệt đối `composeFile`, `composeProject`, và `dockerSudo` nếu cần quyền sudo.
2. Xác minh fingerprint SSH qua kênh tin cậy, cập nhật `sources/known_hosts`.
   Script bật StrictHostKeyChecking; không tự chấp nhận key mới.
3. Source cần Python 3 và Docker Compose. Tài khoản SSH có quyền đọc Docker/file
   hoặc quyền sudo phù hợp. Script không tự sửa group/quyền trên source.
4. Chuẩn bị trusted self-hosted runner có Python 3.10+, OpenSSH, kết nối tới hai
   nguồn; đăng ký labels `self-hosted, linux, x64, nifi-network`.
5. Cấu hình repository Secrets:
   - `NIFI_SOURCE_USERNAME`, `NIFI_SOURCE_PASSWORD`: Basic Auth vào API nguồn.
   - SSH key dùng chung: optional `NIFI_SOURCE_SSH_KEY`, hoặc password riêng
     `NIFI_SOURCE_235_SSH_PASSWORD`, `NIFI_SOURCE_237_SSH_PASSWORD`.
   - Khi dùng password, sudo dùng cùng password; CLI manual hỗ trợ password sudo
     riêng qua `NIFI_SOURCE_SUDO_PASSWORD`. SSH key cần sudo passwordless trong CI.
6. Chạy Actions → **Export NiFi** → **Run workflow**, chọn nguồn.
7. CI export/validate/upload artifact; khi có thay đổi và bật publish, nó tạo
   nhánh export. Mở PR theo compare URL trong log, kiểm tra và merge main.
8. Bàn giao artifact hoặc release/commit đã kiểm tra. Workflow không đổi quyền
   GitHub Actions và không push trực tiếp main.

Các runner test là tạm thời và tự gỡ sau job. Admin cần runner của mình online
cho các lần export sau; nếu không, workflow sẽ chờ runner.

## 3. Admin trên máy khách: tải và sửa đúng một file

Máy khách cần Docker + Compose, Python 3.10+, quyền Docker, đủ RAM/đĩa, kết nối
tới image registry và backend nghiệp vụ. Không cần SSH tới nguồn để deploy.

```bash
git clone https://github.com/danieop/nifi-deploy-kit.git
cd nifi-deploy-kit
# Checkout release/commit đã được bàn giao.
mkdir -p local
chmod 700 local
cp -a bundles/node235 local/node235
chmod 600 local/node235/config.json
nano local/node235/config.json
```

Chọn `node237` nếu cần bản export của máy .237. Hoặc tải artifact CI:

```bash
gh run download RUN_ID -n nifi-export-RUN_ID -D local/downloaded
nano local/downloaded/node235/config.json
```

### Những mục cần chỉnh trong config.json

| Mục JSON | Bắt buộc / cách điền |
|---|---|
| `deployment.projectName` | Có sẵn; đổi thành tên riêng nếu chạy thêm instance trên cùng Docker host |
| `deployment.directory` | Có sẵn `./runtime/node235`, tính từ thư mục chứa config; có thể đổi thành đường dẫn tuyệt đối có quyền ghi |
| `deployment.apiUrl` | URL gateway trên máy đích; giữ host `127.0.0.1`/`localhost`, đổi port cho khớp published port Nginx |
| `deployment.gatewayAuth.username` | Tài khoản UI/API mới, mặc định `nifiadmin` |
| `deployment.gatewayAuth.password` | **Phải thay** `__REQUIRED_SECRET__` bằng mật khẩu máy đích |
| `docker.compose.services.nginx.ports` | Port/bind IP thực tế từ nguồn; chỉnh `published` và `host_ip` theo hạ tầng khách |
| `docker.compose.services.nifi.environment` | Heap, port nội bộ và env nguồn; **thay mọi placeholder** hoặc xóa env không dùng |
| `docker.compose.services.registry` | Có trên node235; kiểm tra cả port Registry để tránh trùng |
| `parameters` | Map tên context → tên parameter → giá trị; sửa endpoint/account/secret theo khách |
| `components[].properties` | Property của processor/service chưa parameter hóa; sửa URL, hostname, port, username/password… tại đây |
| `localFiles` | Khi có placeholder file, điền đường dẫn file cert/key trên máy khách; relative tính từ config.json |

**Nguồn đang dùng heap tối đa 16g.** Bản export giữ giá trị đó. Với máy test nhỏ,
đổi `NIFI_JVM_HEAP_INIT`/`NIFI_JVM_HEAP_MAX` phù hợp, ví dụ `512m`/`2g`; đây không
phải sizing production. Port/bind IP nguồn cũng được giữ nguyên: để UI chỉ local,
đặt `host_ip` của port gateway thành `127.0.0.1`.

Ví dụ các trường cần sửa (chỉ là phần trích, giữ nguyên phần khác của file):

```json
{
  "deployment": {
    "projectName": "nifi-customer-a",
    "directory": "/home/ubuntu/nifi-customer-a",
    "apiUrl": "http://127.0.0.1:9080/nifi",
    "gatewayAuth": {
      "username": "customer-admin",
      "password": "mat-khau-moi-cua-khach",
      "file": "giu-nguyen-duong-dan-export"
    }
  }
}
```

Tên gateway/service có thể khác nếu đã đổi `--nifi-service`/`--gateway-service`
khi export. Dùng tên ở `docker.nifiService` và `docker.gatewayService` để tìm.

### Endpoint và secret NiFi sửa ở đâu?

- Trong `parameters`: tìm context/key đang dùng, sửa giá trị tương ứng.
- Nếu chưa parameter hóa: tìm `components` theo `name`, `groups`, `kind`; sửa
  `properties` của đúng entry. `sourceId` phân biệt các component trùng tên;
  **không sửa sourceId**. Script map ID nguồn sang ID mới sau import.
- IP backend nằm trong parameter/property của Processor hoặc Controller Service.
  NiFi Connection là đường nối giữa component, thường không chứa IP backend.
- `#{Password}` là reference tới parameter: điền secret ở parameter đó; giữ
  reference. Controller-service references được NiFi tự remap, không xuất như
  property để admin ghi đè nhầm ID nguồn vào đích.
- `__REQUIRED_SECRET__` không phải password gốc. NiFi không trả plaintext của
  sensitive values. Export dùng metadata để đánh dấu, rồi admin cấp giá trị mới.
- Sensitive property chưa được đặt ở nguồn được xuất `null`; chỉ điền nếu cần.
  Property `null` có nghĩa để trống/xóa giá trị; parameter phải là chuỗi, xóa key
  khỏi map override nếu không muốn áp dụng nó.
- Secret trong script/URL/ordinary text hoặc binary không được NiFi tự phân loại.
  Cần review trước khi công khai; không có cam kết quét sạch mọi nội dung tùy ý.

File `.htpasswd` nguồn không được copy. Script tạo bcrypt mới từ gatewayAuth.
Cert/key/private keystore chỉ xuất placeholder `localFiles`; admin cấp file mới.

## 4. Chạy deploy

```bash
./deploy.sh --config local/node235/config.json
```

Script kiểm checksum, tạo Docker từ model trong config, import native flow,
áp giá trị đã điền và ghi state/report trong `deployment.directory`. Bản config
đã sửa và runtime chứa secret: giữ tại máy khách, không commit lên repo public.
`local/` đã được gitignore.

Gateway/Docker/localFiles còn placeholder thì script dừng trước khi tạo container.
Placeholder NiFi được phép để lại khi staged: script bỏ qua, ghi đường dẫn vào
`unresolvedSecrets`, không gửi chuỗi placeholder làm password thật.

Mở `deployment.apiUrl` trên máy triển khai, đăng nhập bằng gatewayAuth đã đặt.
Kiểm tra `report.json`, validation và backend RabbitMQ/Elasticsearch/Kafka/API.
**Hạ tầng/import thành công không có nghĩa flow đã chạy nghiệp vụ đúng.**

Khi đã cấp cấu hình và validation đạt:

```bash
./deploy.sh --config local/node235/config.json --start
```

Script từ chối start nếu còn placeholder hoặc processor invalid, enable service
theo dependency, giữ processor vốn DISABLED. Một secret không dùng có thể bỏ
khỏi map override; khi đó script không cố khôi phục giá trị cũ không export được.

## 5. Redeploy

- Dùng lại config và deployment directory: giữ group ID và named volumes;
  không tạo flow trùng.
- Sửa parameter/property: chạy lại lệnh. Component bị sửa phải stopped/disabled;
  giá trị không đổi được bỏ qua, secret đã điền có thể phải ghi lại vì GET bị che.
- Flow hoặc Docker model thay đổi: dừng nguồn vào **máy đích**, stop và drain
  ứng dụng, rồi thêm `--overwrite-config`. Script kiểm tra trước khi recreate;
  giữ group ID và backup native flow trước replacement.
- Đổi gateway credentials/port cùng lúc với overwrite vẫn kiểm tra trạng thái
  đích bằng thông tin truy cập cũ được lưu riêng trong runtime `access.json`.
- Nếu có lỗi, giữ runtime/state để chạy lại. Không `down -v`, prune hoặc xóa
  volume để thử lại. Đổi phiên bản NiFi cần migration riêng.

## 6. Những gì không được clone

Không chuyển queue/FlowFile content, checkpoint/state, provenance, log, Registry
history, dữ liệu trong bind data directories hay dữ liệu hệ thống ngoài NiFi.
Volume nguồn được thay bằng named volume mới; file cấu hình/dependency được copy.
Registry node235 nguồn không có storage mounts nên vẫn ephemeral theo nguồn;
muốn bền vững khi recreate cần admin thêm storage trong model Docker đích.

Registry Clients chỉ inventory, không tái tạo token/client. Không provision
RabbitMQ/Kafka/Elasticsearch/GitLab; không replay Reporting Tasks, Parameter
Providers, Flow Analysis Rules hoặc toàn bộ nifi.properties ngoài startup path.

Baseline: một Docker Compose host, services đang chạy một instance mỗi service,
NiFi HTTP sau Nginx Basic Auth với bind `.htpasswd`. TLS/LDAP/OIDC, cluster,
Swarm/K8s, external networks, Compose secrets/config objects, symlink packaging
và arbitrary bind-mounted NiFi conf không thuộc đường kiểm chứng hiện tại.
Binary ≤64 MiB/file, package assets ≤128 MiB. Những model không hỗ trợ sẽ bị
báo lỗi thay vì giả định đã clone đầy đủ.

CLI cũ của v0.1 vẫn giữ cho bundle schema 1: xem [legacy-v1.md](legacy-v1.md).
