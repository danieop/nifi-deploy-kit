# Các thao tác admin cần làm

## A. Admin quản lý repo/export

1. Chuẩn bị runner tin cậy có Docker/Python và truy cập được mạng NiFi nguồn.
   Đăng ký runner label `nifi-network` theo Settings → Actions → Runners của repo.
   Không cần cài runner trên VM nguồn; có thể dùng một máy quản trị riêng.
2. Cấu hình GitHub Secrets `NIFI_SOURCE_USERNAME`, `NIFI_SOURCE_PASSWORD`.
3. Cấu hình Variables `SOURCE_235_URL`, `SOURCE_237_URL`.
4. Chạy Actions → **Export NiFi** → **Run workflow**, chọn nguồn.
5. Xem kết quả Validate, tải artifact nếu cần. Nếu CI tạo nhánh export, mở PR từ
   URL compare được in trong log; merge PR sau khi kiểm tra, không push main trực tiếp.
6. Chọn/tag commit dùng để bàn giao. Kiểm tra đúng image và dependency trong
   `bundles/<name>/release.json`.

CI chỉ GET cấu hình nguồn. Nó không start/stop processor, không thay parameter,
không đọc queue content và không lấy lại giá trị secret đã bị NiFi che.

## B. Admin trên máy khách

1. Cài Docker + Compose, Python 3.10+; cấp quyền Docker cho tài khoản triển khai.
2. Clone repo, checkout commit/tag đã bàn giao.
3. Copy `examples/deploy.env.example` thành `local/deploy.env`, quyền 600.
4. Điền:
   - Bắt buộc: `COMPOSE_PROJECT_NAME`, `DEPLOY_DIRECTORY` tuyệt đối,
     `NGINX_USERNAME`, `NGINX_PASSWORD`.
   - Có mặc định: `HTTP_PORT=9080`, `BIND_ADDRESS=127.0.0.1`, heap 1g/2g.
     Chọn port không bị chiếm và heap phù hợp RAM. Đổi bind thành `0.0.0.0`
     nếu chủ động cho phép truy cập qua địa chỉ máy khách.
5. Chọn bundle `node235` (kèm Registry) hoặc `node237` (không Registry).
6. Đọc `parameters.required.json`; tạo `local/parameters.local.json` theo context
   thực tế. Điền endpoint và secret của môi trường khách. Không copy dấu che
   `********` thành mật khẩu. Không bắt buộc khai báo parameter không đổi.
7. Chạy `./deploy.sh --bundle bundles/node235 --env local/deploy.env`.
   Thêm `--parameters local/parameters.local.json` khi đã có file override.
8. Mở URL được in ra. Kiểm tra `report.json`, các component invalid, kết nối
   RabbitMQ/Elasticsearch/Kafka và credential. Flow mặc định chưa chạy nghiệp vụ.
9. Khi cấu hình đạt yêu cầu, chạy lại cùng lệnh với `--start` để enable service
   và chạy flow. Command phải từ chối nếu còn processor invalid.
10. Lưu lại local config và deployment directory; đây là thông tin cần cho lần
    redeploy, không commit chúng vào repo public.

## C. Lần triển khai sau

- Cùng checksum: giữ group ID, giữ volume, áp parameter override được cung cấp.
- Flow thay đổi: chuẩn bị/dừng nguồn vào và xử lý hết queue trên **máy đích**,
  sau đó chạy với `--overwrite-config`.
- Không xóa named volumes, không reset state, không đổi khóa mã hóa tùy tiện.
- Nếu thay bản NiFi hoặc chuyển queue/checkpoint từ máy khác: thực hiện migration
  riêng; script từ chối nâng version thông qua redeploy thông thường.
- Nếu script lỗi: giữ nguyên deployment directory, đọc thông báo và report;
  không xóa volume để thử lại. Một lần import đã ghi ID sẽ được tiếp tục bằng ID đó.

## D. Bắt buộc và tùy chọn

| Nội dung | Quy tắc |
|---|---|
| Docker/Compose/Python/quyền Docker | Bắt buộc trên máy triển khai |
| Port và thư mục riêng | Bắt buộc, có default cho port |
| Username/password Nginx | Bắt buộc; script tạo bcrypt `.htpasswd` bằng image httpd |
| Endpoint/secret của flow | Bắt buộc theo component sẽ bật |
| NAR/JAR trong release | Bắt buộc theo dependency, script kiểm checksum |
| Registry | Theo profile bundle |
| GitLab Registry Clients và token | Chỉ cần khi muốn dùng chức năng registry; cấu hình riêng |
| TLS/LDAP/OIDC | Ngoài baseline HTTP + Nginx Basic Auth hiện tại |
| Tự chạy flow | Tùy chọn `--start`, mặc định staged |
| Di chuyển dữ liệu/state của nguồn | Ngoài scope triển khai mới |

Runner dùng để test có thể là runner tạm. Workflow export sau đó sẽ chờ nếu
không có runner online với label `nifi-network`; admin cần giữ runner của mình
hoạt động cho các lần export tiếp theo.
