CREATE USER {{db_user}} WITH ENCRYPTED PASSWORD '{{db_pass}}';
ALTER USER {{db_user}} CREATEDB;
