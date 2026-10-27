# 笔记：IK 分词与全文索引
topic: 全文索引

OceanBase / seekdb 的全文索引支持 WITH PARSER ik，中文按词典切词，英文按空格与标点切分。

建表时可以同时声明 FULLTEXT KEY 与 VECTOR 列，查询时用 MATCH ... AGAINST 做全文匹配，用 cosine_distance 做向量距离，两者可以分别召回再用 RRF 融合。

注意全文索引的维护成本：每次大批量导入后建议观察索引大小与查询延迟。
