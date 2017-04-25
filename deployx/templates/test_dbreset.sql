DROP DATABASE IF EXISTS test_{{db_name}};
CREATE DATABASE test_{{db_name}} WITH OWNER {{db_user}} ENCODING = 'UTF8' LC_CTYPE = '{{locale}}'
  LC_COLLATE = '{{locale}}' TEMPLATE template0;
