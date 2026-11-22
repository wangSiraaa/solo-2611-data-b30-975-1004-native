case.json每个basket对象是独立实物件，unit_id a/b/c与退货请求的b对应；金额为整数分。before_coupons是先发布快照，added_coupons加入后形成另一合法快照。grade按unit_id给出；原价与优惠D均为金额，requires是SKU件数。材料不提供优化器或售后实现。

双面券纸扩展：`register_sheet` 登记网格券纸与逐小边穿孔，`try_tear` 试撕（不动纸），`quote_group` 对最多5个同时结账的购物篮求合计最低并绑定每篮旧定序报价，`commit_group` 原子交纸并登记销售（退货按原逐件实付）。详见 SOLUTION.md。
