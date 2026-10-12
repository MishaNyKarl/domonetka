set -eu
stage=/opt/domonetka-stage-sso-0.1.2-20261009
backup=/root/domonetka-backup-sso-0.1.2-20261009
python=/opt/domonetka/.venv-sso-0.1.2/bin/python
"$python" "$stage/preflight.py"
"$python" -m py_compile "$stage/server.py" "$stage/sso_bridge.py"
mkdir -p "$backup/public"
chmod 700 "$backup"
for name in server.py sso_bridge.py binom_sync.py; do cp "/opt/domonetka/$name" "$backup/$name"; done
for name in admin.html admin.js sso-ui.js styles.css; do cp "/opt/domonetka/public/$name" "$backup/public/$name"; done
cp /etc/systemd/system/domonetka.service.d/20-sso.conf "$backup/20-sso.conf"
systemctl stop domonetka
trap 'systemctl start domonetka' EXIT
"$python" - <<'PY'
import sqlite3,os
src=sqlite3.connect('/opt/domonetka/data/domonetka.sqlite')
dest=sqlite3.connect('/root/domonetka-backup-sso-0.1.2-20261009/domonetka.sqlite')
src.backup(dest);dest.close();src.close()
os.chmod('/root/domonetka-backup-sso-0.1.2-20261009/domonetka.sqlite',0o600)
PY
for name in server.py sso_bridge.py; do install -o domonetka -g domonetka -m 640 "$stage/$name" "/opt/domonetka/$name"; done
for name in admin.html admin.js sso-ui.js; do install -o domonetka -g domonetka -m 640 "$stage/public/$name" "/opt/domonetka/public/$name"; done
install -o root -g root -m 644 "$stage/domonetka-sso.conf" /etc/systemd/system/domonetka.service.d/20-sso.conf
systemctl daemon-reload
systemctl start domonetka
trap - EXIT
curl --retry 5 --retry-delay 1 --retry-connrefused -fsS http://127.0.0.1:8097/health
curl -fsS http://127.0.0.1:8097/api/auth/config
"$python" - <<'PY'
import sqlite3
old=sqlite3.connect('/root/domonetka-backup-sso-0.1.2-20261009/domonetka.sqlite')
new=sqlite3.connect('/opt/domonetka/data/domonetka.sqlite')
for table,fields in [('users','id,username,role,active,password_hash,local_login_enabled'),
                     ('streams','id,owner_id'),('offers','id,stream_id'),('trackers','id,user_id')]:
    a=old.execute('SELECT '+fields+' FROM '+table+' ORDER BY 1').fetchall()
    b=new.execute('SELECT '+fields+' FROM '+table+' ORDER BY 1').fetchall()
    assert a==b,table
    print(table,len(b),'unchanged')
assert new.execute('SELECT count(*) FROM ov_sso_identities').fetchone()==old.execute('SELECT count(*) FROM ov_sso_identities').fetchone()
print('Identity count unchanged; new linking still requires owner confirmation')
PY
