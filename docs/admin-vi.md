# Triển khai NiFi trên máy khách

Admin sửa một `config.json` chứa cấu hình Docker, endpoint và giá trị NiFi.
Bundle phải gồm đủ `flow.json`, `config.json`, `assets/`.

## 1. Chuẩn bị máy đích

- Docker + Compose, Python 3.10+, quyền Docker cho tài khoản triển khai.
- RAM/đĩa phù hợp, thư mục có quyền ghi và port không bị chiếm.
- Kết nối tới image registry để tải NiFi/Nginx và image phụ trợ; nếu dùng private
  registry, cấp quyền Docker login tương ứng.
- Kết nối từ container NiFi tới các backend mà flow sử dụng, như RabbitMQ, Kafka,
  Elasticsearch và HTTP/API. Backend phải có sẵn; script không cài các hệ thống này.

Admin có thể SSH vào máy đích để chạy script. Máy đích không cần SSH sang máy
nguồn vì toàn bộ cấu hình/dependency đã nằm trong bundle.

## 2. Tải bundle

```bash
git clone https://github.com/danieop/nifi-deploy-kit.git
cd nifi-deploy-kit
mkdir -p local
chmod 700 local

# Thay RUN_ID bằng ID lần export được bàn giao; gh cần đăng nhập.
gh run download RUN_ID --repo danieop/nifi-deploy-kit \
  -n nifi-export-RUN_ID -D local/customer-a
chmod 600 local/customer-a/config.json
nano local/customer-a/config.json
```

Artifact chứa các file ngay ở root thư mục tải về. Dùng thư mục riêng cho từng
lần tải để giữ cấu hình đã chỉnh. Hoặc copy một bundle có sẵn:

```bash
cp -a bundles/node235 local/customer-a
```

Chọn một cách lấy bundle. Các bundle node235/node237 trong repo là dữ liệu mẫu;
server nguồn của workflow được nhập khi chạy. Để tự export nguồn, xem
[workflow GitHub](export-workflow.md) hoặc
[export trực tiếp trên Ubuntu/NetBird](export-ubuntu-netbird.md).

## 3. Sửa config.json

| Trường | Việc admin cần làm |
|---|---|
| `deployment.projectName` | Tên Compose project riêng cho instance; có giá trị mặc định |
| `deployment.directory` | Runtime directory có quyền ghi; relative tính từ config.json, hoặc dùng đường dẫn tuyệt đối |
| `deployment.apiUrl` | URL gateway local, dùng localhost/127.0.0.1; port khớp published port của gateway |
| `deployment.gatewayAuth.username/password` | Đặt tài khoản UI/API mới; password bắt buộc thay placeholder |
| `docker.compose.services` | Kiểm tra image, ports, bind IP, mounts, environment, heap và resource limits |
| `parameters` | Sửa giá trị theo tên context và parameter: endpoint, tài khoản, secret… |
| `components[].properties` | Sửa property processor/service chưa parameter hóa |
| `localFiles` | Điền đường dẫn file cert/key trên máy khách nếu có placeholder |

Dùng `docker.nifiService`/`docker.gatewayService` để tìm đúng service trong model.
Hai bundle mẫu giữ heap nguồn 1g/16g; chỉnh theo RAM máy khách. Ví dụ test nhỏ có
thể chọn `NIFI_JVM_HEAP_INIT=512m`, `NIFI_JVM_HEAP_MAX=2g`; đây không phải sizing
production. Port/bind IP được giữ từ nguồn. Đặt `host_ip=127.0.0.1` nếu chỉ muốn
truy cập UI tại máy đích, hoặc chọn bind IP phù hợp hạ tầng khách. Kiểm tra cả
port các service khác như NiFi Registry nếu có.

### Endpoint và secret

- Tìm endpoint trong `parameters` hoặc `components[].properties`. Ví dụ RabbitMQ
  dùng Host Name/Port/Virtual Host, Kafka dùng bootstrap.servers, Elasticsearch
  dùng HTTP Hosts. IP không tự đổi sang IP hạ tầng mới.
- Backend phải truy cập được từ **container NiFi**: DNS/IP, port, firewall và
  credential phải đúng. `localhost` trong container trỏ vào chính container đó.
- `groups`, `kind`, `name` giúp nhận diện component; giữ nguyên `sourceId` để
  script map sang ID trên NiFi đích. NiFi tự remap controller-service references.
- `#{Password}` là parameter reference: điền giá trị trong parameter tương ứng,
  giữ reference trong property.
- `__REQUIRED_SECRET__` là placeholder. Giá trị sensitive gốc không được API trả
  lại; admin cấp giá trị mới. Không dùng chuỗi che `********` làm password thật.
- Sensitive property chưa cấu hình có giá trị `null`; chỉ điền nếu cần. Property
  null nghĩa là để trống/xóa; parameter overrides phải là chuỗi. Bỏ key parameter
  khỏi map nếu không muốn override nó.
- File `.htpasswd` được tạo mới từ gatewayAuth. Private cert/key/keystore chỉ
  có placeholder localFiles, admin cấp file tại máy khách.

Giữ local config và runtime riêng, không commit secret vào repo public. Thư mục
`local/` đã được gitignore. Nội dung script/URL/text/binary không được bảo đảm
scrub toàn bộ credential; chỉ dùng nội dung được phép chia sẻ trên repo này.

## 4. Deploy và chạy flow

```bash
./deploy.sh --config local/customer-a/config.json
```

Script kiểm checksum, dựng Docker từ model đã export, import native flow, áp
cấu hình đã điền và ghi `report.json`/deployment state trong runtime directory.
Gateway/Docker/localFiles còn placeholder sẽ bị từ chối trước khi tạo container.
Placeholder NiFi được bỏ qua khi staged, được liệt kê trong `unresolvedSecrets`.

Mở `deployment.apiUrl`, đăng nhập bằng gatewayAuth. Đọc report, xử lý validation,
kiểm tra backend trước khi chạy nghiệp vụ. Hạ tầng/import thành công không tự
chứng minh flow chạy nghiệp vụ đúng.

```bash
./deploy.sh --config local/customer-a/config.json --start
```

`--start` từ chối khi còn placeholder hoặc processor invalid. Services được
bật theo dependency; processor vốn DISABLED được giữ disabled. Secret không dùng
có thể bỏ khỏi map override, không có giá trị gốc để tự khôi phục trên máy mới.

## 5. Redeploy

Dùng lại config/runtime directory để giữ group ID và volumes. Parameter/property
đã sửa được áp vào component stopped/disabled; giá trị không đổi được bỏ qua.
Khi nhận bundle mới, chuyển các giá trị riêng của khách sang config mới trước khi
triển khai; export không tự cập nhật file local hay NiFi đang chạy.

Thay flow, Docker model hoặc packaged assets: dừng nguồn dữ liệu vào **máy đích**,
stop/drain ứng dụng rồi chạy:

```bash
./deploy.sh --config local/customer-a/config.json --overwrite-config
```

Script kiểm trạng thái trước khi recreate, giữ group ID và lưu native flow backup.
Thông tin gateway cũ được giữ riêng trong runtime để kiểm tra khi đổi port/auth.
Giữ `owner.json`, `deployment.json`, `access.json` và named volumes; không xóa
runtime, `down -v` hoặc prune để thử lại khi lỗi. Đổi phiên bản/image NiFi cần
migration riêng.

Không chuyển queue/content/checkpoint/provenance, dữ liệu bind directories hay
Registry history từ nguồn. Registry không có storage mount sẽ là ephemeral;
muốn persistence phải cấu hình storage đích. Registry Clients/tokens và các
integration ngoài baseline cần cấu hình riêng. Chi tiết trường dữ liệu:
[config-reference.md](config-reference.md).
