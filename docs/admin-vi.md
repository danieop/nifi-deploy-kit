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

## 2. Export server bất kỳ bằng GitHub Actions

Không cần sửa code hoặc khai báo IP trong repo. Một lần chạy export một server.

1. Chuẩn bị trusted self-hosted Linux/x64 runner có Python 3.10+, OpenSSH và
   kết nối tới SSH/API của nguồn. Runner có label `nifi-network`, hoặc label riêng
   do bạn nhập ở `runner_label` khi chạy workflow.
2. Tạo GitHub Secret dạng JSON cho nguồn mới, ví dụ `CUSTOMER_A_EXPORT`, chứa
   credential API/SSH và SSH host key đã xác minh. Dùng mẫu
   `examples/export-credentials.example.json`. Không nhập password vào form workflow.
3. Mở **Actions → Export NiFi → Run workflow**, chọn branch **main**, điền:

   | Trường | Ví dụ |
   |---|---|
   | `bundle_name` | `customer-a-prod` — tên bất kỳ, chữ thường/số/dấu gạch ngang |
   | `nifi_url` | `http://nifi.customer.lan:9080/nifi` |
   | `docker_host` | `ubuntu@nifi.customer.lan` |
   | `compose_file` | `/opt/nifi/compose.yml` trên nguồn |
   | `credentials_secret` | `CUSTOMER_A_EXPORT` — tên Secret đã tạo |
   | `docker_sudo` | Bật nếu tài khoản cần sudo để đọc Docker |
   | `publish` | Bật nếu muốn tạo nhánh cập nhật bundle trong repo |

4. Các trường còn lại có mặc định; chỉ đổi nếu khác hạ tầng: Compose project,
   SSH port, tên service NiFi/Nginx, group ID và runner label.
5. CI chỉ đọc nguồn, validate rồi upload artifact `nifi-export-RUN_ID`. Artifact
   chứa đúng bundle của lần này ở root: `flow.json`, `config.json`, `assets/`.
6. Nếu bật publish và có thay đổi, CI tạo nhánh chỉ chứa cập nhật
   `bundles/<bundle_name>`. Mở PR từ compare URL trong log, kiểm tra và merge main.
   Không thay đổi thì không tạo nhánh; artifact vẫn có ở mỗi lần thành công.
7. Main, tag release, file local đã sửa và NiFi đang chạy ở khách không tự thay
   theo workflow. Admin phải chọn bundle mới và chủ động deploy lại.

Cách tạo credential profile, chạy bằng gh và đổi nguồn:
[xem hướng dẫn workflow](export-workflow.md). Không còn dùng `sources/hosts.json`,
Secrets có hậu tố 235/237 hay Variables `SOURCE_235_URL`/`SOURCE_237_URL`.

Source vẫn cần Python 3, Docker Compose, quyền Docker/file hoặc sudo phù hợp.
Workflow không tự cài phần mềm hay sửa quyền trên source. Các runner test là
runner tạm; admin cần runner online cho lần export sau.

## 3. Admin trên máy khách: tải và sửa đúng một file

Máy khách cần Docker + Compose, Python 3.10+, quyền Docker, đủ RAM/đĩa, kết nối
tới image registry và backend nghiệp vụ. Admin chạy deploy.sh trên máy đích (có thể SSH vào máy đích để thao tác). Máy đích
không cần SSH sang nguồn: cấu hình đã có trong bundle.

```bash
git clone https://github.com/danieop/nifi-deploy-kit.git
cd nifi-deploy-kit
git checkout v0.3.0
mkdir -p local
chmod 700 local
cp -a bundles/node235 local/node235
chmod 600 local/node235/config.json
nano local/node235/config.json
```

Hai bundle `node235`/`node237` chỉ là ví dụ đã bàn giao. Để dùng nguồn vừa export,
tải artifact CI. Từ workflow mới, các file nằm ngay ở root thư mục tải về:

```bash
gh run download RUN_ID --repo danieop/nifi-deploy-kit \
  -n nifi-export-RUN_ID -D local/export-RUN_ID
nano local/export-RUN_ID/config.json
./deploy.sh --config local/export-RUN_ID/config.json
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
