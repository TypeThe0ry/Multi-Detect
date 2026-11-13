# r10 冻结四视角错误审计

日期：2026-11-08。范围限定为离线复核 `artifacts/r21id-r10/evaluation/r3-four-view/` 中已有的成对预测和报告；未运行推理、训练或评测服务，没有改动生产模型、准入门槛或 `artifacts/`。

## 结论

r10 未达到冻结四视角的两项硬门：实例级跨视角一致率为 **23/30 = 0.7667**（门槛 ≥0.90，至少需 27/30）；显式干扰物接受率为 **68/1,166 = 0.0583**（门槛 ≤0.026，按该分母最多允许 30 条）。r3 也未达到这两项门槛：**22/30 = 0.7333**、**110/1,166 = 0.0943**。r10 比 r3 多 1 个一致实例、少 42 条误收，但仍有 4 个一致实例和 38 条误收的差距。维持现有结论：r10 不准入替换，生产模型保持 r21id-r3。

## 证据与计分口径

- 冻结集：[`r15-additions/test.jsonl`](../artifacts/r21id-r3/pipeline/r15-additions/test.jsonl)，2,204 行、sample_id 唯一。r3 和 r10 各有 2,204 条预测，sample_id 集与数据集完全相同；其中 93 行无 target_id，按 evaluator 规则不计分，剩余 2,111 行可连接到数据集的 `target_id`、`world`、`view` 和 `label_type`。
- 指标实现：[`eval_r21id_identity.py`](../scripts/eval_r21id_identity.py)。实例一致性按完整 `front / oblique / edge_truncated / recovery` 四视角分组；预测 `confirm_target` 或 `approach_track` 都计为接受，`reject_track` 计为拒绝。显式干扰物分母取 `label_type` 含 `distractor` 且 `view` 非空的行。
- `identity-report.json` 记有 93 行无 target_id（不计分），另有 67 行 view=null；view=null 不参与这两个四视角率。下面的误收明细只列入门槛分母的非空 view 样本。
- 离线复核直接调用上述评测模块的 JSON 读取、sample_id 合并与指标计算函数；重算的点估计与冻结报告一致。

| 指标 | r21id-r3 | r21id-r10 | 门槛 |
|---|---:|---:|---:|
| 实例级跨视角一致 | 22/30 = 0.7333 | 23/30 = 0.7667 | ≥0.90 |
| 显式干扰物接受 | 110/1,166 = 0.0943 | 68/1,166 = 0.0583 | ≤0.026 |

## 跨视角不一致实例

两模型各有 30 个完整四视角实例。r3 有 8 个不一致实例，r10 有 7 个。以下列出至少一个模型不一致的 target_id；`target_id / world / view / sample_id` 来自冻结集，决策来自对应预测文件。

| target_id | world | view | sample_id | r3 decision | r10 decision |
|---|---|---|---|---|---|
| `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `front` | `f9aa1e0a17bae5f06b80890c` | `reject_track` | `reject_track` |
| `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `oblique` | `d733a4d4d92c33cff38f1272` | `confirm_target` | `reject_track` |
| `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `edge_truncated` | `cdddcdf4c3c6b5706c0bf126` | `reject_track` | `reject_track` |
| `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `recovery` | `cb19efb9dfb25ec02b0b0744` | `reject_track` | `reject_track` |
| `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `ffe37f7a581ef2ec27ddbb99` | `confirm_target` | `confirm_target` |
| `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `oblique` | `da267bda83fa44a120f5d071` | `confirm_target` | `confirm_target` |
| `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `edge_truncated` | `ff356126d1b9d518fa16a2e4` | `reject_track` | `reject_track` |
| `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `recovery` | `ea4c5efb7f2ec4dec8fc43cb` | `reject_track` | `reject_track` |
| `confuser_0010_00_00` | `gen_r21id_r3_w0010` | `front` | `eeca40b091785ed42f826f25` | `reject_track` | `reject_track` |
| `confuser_0010_00_00` | `gen_r21id_r3_w0010` | `oblique` | `e2a1d6393fce8beaff3c408d` | `confirm_target` | `confirm_target` |
| `confuser_0010_00_00` | `gen_r21id_r3_w0010` | `edge_truncated` | `f586ba35806aedec2520c5e7` | `reject_track` | `reject_track` |
| `confuser_0010_00_00` | `gen_r21id_r3_w0010` | `recovery` | `d47f3033fd3c9ca6aebc895e` | `reject_track` | `reject_track` |
| `distractor_0000_04` | `gen_r21id_r3_w0000` | `front` | `ed331d64c265160f3ed02213` | `reject_track` | `reject_track` |
| `distractor_0000_04` | `gen_r21id_r3_w0000` | `oblique` | `fbc46a5034227a3e81113c4e` | `reject_track` | `reject_track` |
| `distractor_0000_04` | `gen_r21id_r3_w0000` | `edge_truncated` | `f01768b45c04411dfefd9a52` | `reject_track` | `reject_track` |
| `distractor_0000_04` | `gen_r21id_r3_w0000` | `recovery` | `bdea57dfd4b4fffb697b073f` | `reject_track` | `approach_track` |
| `distractor_0010_01` | `gen_r21id_r3_w0010` | `front` | `f3262eb74d0354d156e3cc26` | `reject_track` | `reject_track` |
| `distractor_0010_01` | `gen_r21id_r3_w0010` | `oblique` | `eff1d8de54b1638558b82f03` | `reject_track` | `reject_track` |
| `distractor_0010_01` | `gen_r21id_r3_w0010` | `edge_truncated` | `f7c28eb7b0d29544b900694e` | `reject_track` | `reject_track` |
| `distractor_0010_01` | `gen_r21id_r3_w0010` | `recovery` | `e133f1e6ef1f596378ed451d` | `approach_track` | `approach_track` |
| `target_true_gen_r21id_r3_w0000` | `gen_r21id_r3_w0000` | `front` | `e14d8c2d1c7d2d3ead7bf701` | `confirm_target` | `confirm_target` |
| `target_true_gen_r21id_r3_w0000` | `gen_r21id_r3_w0000` | `oblique` | `b20c92991f3265f1d2395649` | `confirm_target` | `confirm_target` |
| `target_true_gen_r21id_r3_w0000` | `gen_r21id_r3_w0000` | `edge_truncated` | `fc3b09161fd6cdd62367a44d` | `reject_track` | `reject_track` |
| `target_true_gen_r21id_r3_w0000` | `gen_r21id_r3_w0000` | `recovery` | `f4dabbffaa1ead7b02ea3c1c` | `confirm_target` | `confirm_target` |
| `target_true_gen_r21id_r3_w0001` | `gen_r21id_r3_w0001` | `front` | `fe9a8487c7a095e617e3ac65` | `confirm_target` | `confirm_target` |
| `target_true_gen_r21id_r3_w0001` | `gen_r21id_r3_w0001` | `oblique` | `fe87b2338d7527683cab5a7d` | `reject_track` | `reject_track` |
| `target_true_gen_r21id_r3_w0001` | `gen_r21id_r3_w0001` | `edge_truncated` | `f55d2456ac3f53ef8121da43` | `confirm_target` | `confirm_target` |
| `target_true_gen_r21id_r3_w0001` | `gen_r21id_r3_w0001` | `recovery` | `f12fc1bf4c87cbf26ac353a1` | `confirm_target` | `confirm_target` |
| `target_true_gen_r21id_r3_w0002` | `gen_r21id_r3_w0002` | `front` | `fdd41335dbd313fcae1cfb31` | `reject_track` | `reject_track` |
| `target_true_gen_r21id_r3_w0002` | `gen_r21id_r3_w0002` | `oblique` | `c05dba5c680f65625046fd59` | `confirm_target` | `confirm_target` |
| `target_true_gen_r21id_r3_w0002` | `gen_r21id_r3_w0002` | `edge_truncated` | `fdf49f187ae2b057d66600de` | `reject_track` | `reject_track` |
| `target_true_gen_r21id_r3_w0002` | `gen_r21id_r3_w0002` | `recovery` | `f57aa0d07cd351236200663d` | `confirm_target` | `confirm_target` |
| `target_true_gen_r21id_r3_w0026` | `gen_r21id_r3_w0026` | `front` | `f73159835dfe772f699bf3f2` | `confirm_target` | `confirm_target` |
| `target_true_gen_r21id_r3_w0026` | `gen_r21id_r3_w0026` | `oblique` | `fd9f4bc2515f3188e03b8fe5` | `approach_track` | `confirm_target` |
| `target_true_gen_r21id_r3_w0026` | `gen_r21id_r3_w0026` | `edge_truncated` | `eb9a460d9a69cff1d7584ee3` | `reject_track` | `confirm_target` |
| `target_true_gen_r21id_r3_w0026` | `gen_r21id_r3_w0026` | `recovery` | `c506fcae320dd346d5cef562` | `approach_track` | `confirm_target` |

不一致目标的对照：r10 修复了 `confuser_0000_00_00`（`gen_r21id_r3_w0000`）和 `target_true_gen_r21id_r3_w0026`（`gen_r21id_r3_w0026`）；r3 中一致、r10 新增不一致的是 `distractor_0000_04`（`gen_r21id_r3_w0000`，仅 recovery 从 reject_track 变为 approach_track）。其余 6 个不一致 target_id 两模型共有：`confuser_0001_00_00`、`confuser_0010_00_00`、`distractor_0010_01`、`target_true_gen_r21id_r3_w0000`、`target_true_gen_r21id_r3_w0001`、`target_true_gen_r21id_r3_w0002`。

表中所有四视角 sample_id 都保留，因而可以直接看到偏差落在哪一视角：干扰物实例 `distractor_0010_01` 两模型都是 recovery 误接受；真目标实例 `target_true_gen_r21id_r3_w0000` 都在 edge_truncated 被拒，而 `target_true_gen_r21id_r3_w0001` 都在 oblique 被拒。`target_true_gen_r21id_r3_w0002` 两模型均呈现 front/edge_truncated 拒绝、oblique/recovery 接受的 2:2 分裂。

## 显式干扰物误收样本

共有 115 个 sample_id 至少被一个模型接受：两模型都误收 63 条，只有 r3 误收 47 条，只有 r10 误收 5 条。表中原始 decision 用来展示配对差异；`approach_track` 与 `confirm_target` 都属于该门槛中的接受。

| 对照 | sample_id | target_id | world | view | label_type | r3 decision | r10 decision |
|---|---|---|---|---|---|---|---|
| r3-only | `016395a3dcb08f6b568c430f` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `02ce7f38a0c14ab3bd3465e6` | `confuser_0017_00_00` | `gen_r21id_r3_w0017` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `02eb4e8a443cabe2b0a3a6b5` | `distractor_0009_03` | `gen_r21id_r3_w0009` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `038e4b4682749947f289f069` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `edge_truncated` | `proposal_distractor` | `approach_track` | `approach_track` |
| r3-only | `05cf492954f011d5561f5555` | `distractor_0009_03` | `gen_r21id_r3_w0009` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `063877da6dfbbb1178086554` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `edge_truncated` | `proposal_distractor` | `approach_track` | `confirm_target` |
| 两者误收 | `0669333dfd17a22943e11f02` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `08e4dda175953fb592b7dd53` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `0969938efe7a509c1bf9ac68` | `confuser_0008_00_00` | `gen_r21id_r3_w0008` | `recovery` | `distractor` | `confirm_target` | `reject_track` |
| r3-only | `0f1a91420b47a58230e9d65d` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `recovery` | `distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `11c9bf837c918a6182049640` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `1210ae71b1266b21201fcc2e` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `recovery` | `proposal_distractor` | `approach_track` | `approach_track` |
| r10-only | `18151d423a4d42e56f5183b3` | `distractor_0008_04` | `gen_r21id_r3_w0008` | `edge_truncated` | `proposal_distractor` | `reject_track` | `confirm_target` |
| r3-only | `1c0759ff07825beea62f21c6` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `recovery` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `2026adb6666f0dbc4880f8f3` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `2122aab7047a9fe6635f094e` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `22163d330e54df146e8a3123` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `27a0177c0a1f8d917bd39523` | `confuser_0017_00_00` | `gen_r21id_r3_w0017` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `27d7a9d41cb54ec7498c8274` | `confuser_0024_00_00` | `gen_r21id_r3_w0024` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `2bb6a3ac744c291182a19a6f` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `2ec38c6476d0b0bda48e63b4` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `31d67879f05825d461f9e605` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `330239e4b6e851966ee0d66d` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `344eb888904901166482398c` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `36eda78be9b031a947b37e27` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `3b1249a7367f6c04a65904dd` | `distractor_0000_04` | `gen_r21id_r3_w0000` | `edge_truncated` | `distractor` | `approach_track` | `reject_track` |
| r3-only | `3ce0a74a32ed0303db496d5c` | `distractor_0000_04` | `gen_r21id_r3_w0000` | `recovery` | `distractor` | `approach_track` | `reject_track` |
| 两者误收 | `3d830a98715e41903bdd2477` | `confuser_0010_00_00` | `gen_r21id_r3_w0010` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `4169ca391c06008673f0d152` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `recovery` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `42a963bd9cafe753f67baa88` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `oblique` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `4926a744f02f357ce0150449` | `confuser_0018_00_00` | `gen_r21id_r3_w0018` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `49ec69e2810db38f632061dd` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `4b15df6b7e547e69cc1338de` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `554763ebc3fe7b3507a8e5a1` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `5832880f5d49a35d22ec18f5` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `58b1ef0aa049fb7191378b1d` | `confuser_0008_00_00` | `gen_r21id_r3_w0008` | `oblique` | `distractor` | `confirm_target` | `reject_track` |
| r3-only | `59f2ead12075ded852f39cdc` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `oblique` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `5c88ac80c705f5420a06c1f7` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `5d732f6ab92bb8e1a7541e14` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `60fabdbc0ee2d42d0e0bccf4` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `62a6b500f3af1cee499704fe` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `62f7205ea56c7b3ca288791e` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `edge_truncated` | `proposal_distractor` | `approach_track` | `approach_track` |
| 两者误收 | `64549ccce5a624cc059a1879` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `edge_truncated` | `proposal_distractor` | `approach_track` | `approach_track` |
| 两者误收 | `6485133d64ce4c22a32f7820` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r10-only | `69a6759118ba8a9cc52dc462` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `recovery` | `proposal_distractor` | `reject_track` | `confirm_target` |
| 两者误收 | `69d79f329b27d3f5b168ed08` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `6a272bfa3c5e4a2ef26ed489` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `edge_truncated` | `distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `6af1222c6f99a1d39499f534` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `distractor` | `confirm_target` | `confirm_target` |
| r10-only | `6c1254be83029ea27427ef9a` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `recovery` | `proposal_distractor` | `reject_track` | `confirm_target` |
| 两者误收 | `70e9a378c196c3c409d8623d` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `75bf594c83c4bd2eb48c8cfc` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `oblique` | `distractor` | `confirm_target` | `reject_track` |
| r3-only | `77aa9c9190927f3be577f615` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `7806720229df57d95907dc40` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `78af3fd2e220e3ef983764b2` | `distractor_0009_03` | `gen_r21id_r3_w0009` | `recovery` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `79020585a05b36d6ad622b67` | `confuser_0008_00_00` | `gen_r21id_r3_w0008` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `7944952467561d91eddc3c88` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `7a4396fdd52fb4886fd01a71` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `distractor` | `confirm_target` | `reject_track` |
| r3-only | `7af488f336f374159987429f` | `confuser_0017_00_00` | `gen_r21id_r3_w0017` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `7c0e0f49fed623784e1793de` | `confuser_0010_00_00` | `gen_r21id_r3_w0010` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r10-only | `7d2b5315baa710abceb5b351` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `proposal_distractor` | `reject_track` | `confirm_target` |
| r3-only | `7f97969c52c5feb6f75da9bd` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `edge_truncated` | `distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `8304df269a93cd55086fe279` | `distractor_0000_04` | `gen_r21id_r3_w0000` | `edge_truncated` | `distractor` | `approach_track` | `approach_track` |
| r3-only | `8670afcda9466aa3c2e922af` | `confuser_0018_00_00` | `gen_r21id_r3_w0018` | `front` | `proposal_distractor` | `approach_track` | `reject_track` |
| r3-only | `86ac822e5057cd55acd76d3b` | `distractor_0009_03` | `gen_r21id_r3_w0009` | `recovery` | `distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `89fe162ceedc743360ab7a75` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `9047a9c7478c3092faa226c4` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `recovery` | `proposal_distractor` | `approach_track` | `reject_track` |
| r3-only | `93564d3aa901f753ed392b1c` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `99c3e9620b9e47d92ff133b1` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `9a09cbe2fd2eb45673553078` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `9a512ed380c1d1b00adc2ab3` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `9d085642f62875f7fc31a709` | `confuser_0010_00_00` | `gen_r21id_r3_w0010` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `a12f18bcb354c0db65095947` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `a1c04494c1f5889190fb74fa` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `a216c54784d9761f9a66347a` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `recovery` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `a34fb49b8f41faa185e10045` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `a6bab77f849b7fcd074e5721` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `edge_truncated` | `distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `a7bbb31b0425364e5c5fabc8` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `distractor` | `confirm_target` | `confirm_target` |
| r3-only | `a93f3566896c26b274d35acc` | `confuser_0008_00_00` | `gen_r21id_r3_w0008` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `ac56f931370a20952550fb37` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `b2fef74e2c8f806086888179` | `confuser_0008_00_00` | `gen_r21id_r3_w0008` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `b3d25953c5b80bc0a6c1c7e3` | `confuser_0018_00_00` | `gen_r21id_r3_w0018` | `front` | `proposal_distractor` | `approach_track` | `confirm_target` |
| r3-only | `b60098ece30331687a6fb5f8` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `edge_truncated` | `distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `b7dc06647efcfe4c40c89404` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `recovery` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `bad42415c4c90316ad16dd5e` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `bcf02abdc20205d55c1a8a70` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r10-only | `bdea57dfd4b4fffb697b073f` | `distractor_0000_04` | `gen_r21id_r3_w0000` | `recovery` | `proposal_distractor` | `reject_track` | `approach_track` |
| r3-only | `bf4adce34e76d2f34da065c7` | `distractor_0009_03` | `gen_r21id_r3_w0009` | `front` | `distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `bf4dd99cde0f1618f193b52e` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `bf71ab96a701f06b9847010e` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `distractor` | `confirm_target` | `reject_track` |
| r3-only | `bff0a4b010661ba6be0b97ef` | `distractor_0000_04` | `gen_r21id_r3_w0000` | `front` | `distractor` | `approach_track` | `reject_track` |
| r3-only | `c2c746463f6e8a003c14aafc` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `c75dd87d87e1cd039e16edca` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `c834a7faf410d767cdcd71f9` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `edge_truncated` | `distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `c90995eed3b393ac5e674c87` | `confuser_0018_00_00` | `gen_r21id_r3_w0018` | `front` | `proposal_distractor` | `approach_track` | `approach_track` |
| 两者误收 | `cbc9de16ac87a3a3a18442c7` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `edge_truncated` | `proposal_distractor` | `approach_track` | `approach_track` |
| 两者误收 | `d127172dbaf0cc7972c303dc` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `d325669d08d42372217464d8` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `d733a4d4d92c33cff38f1272` | `confuser_0000_00_00` | `gen_r21id_r3_w0000` | `oblique` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `d7f8cabf2da9692df5d4b2ca` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `da267bda83fa44a120f5d071` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `da3af9a59ed4103e8bdc6a80` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `edge_truncated` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `dd4922dccf800c4502530d2e` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `ddba1a1ccb50bc541dc6da49` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `de7e4719548ebd684cb084f5` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `e133f1e6ef1f596378ed451d` | `distractor_0010_01` | `gen_r21id_r3_w0010` | `recovery` | `proposal_distractor` | `approach_track` | `approach_track` |
| 两者误收 | `e2a1d6393fce8beaff3c408d` | `confuser_0010_00_00` | `gen_r21id_r3_w0010` | `oblique` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `e4fb48533f7694c2a2caa8ed` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `e6f8b54a9102f9e2fdf35234` | `confuser_0002_00_00` | `gen_r21id_r3_w0002` | `front` | `proposal_distractor` | `confirm_target` | `confirm_target` |
| r3-only | `e88048557d95bb313f6f036a` | `confuser_0026_00_00` | `gen_r21id_r3_w0026` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `ea90091cd383de9e9cda1ba6` | `distractor_0001_03` | `gen_r21id_r3_w0001` | `front` | `proposal_distractor` | `approach_track` | `reject_track` |
| r3-only | `eab2955c35489e50ba98f88f` | `distractor_0009_03` | `gen_r21id_r3_w0009` | `recovery` | `proposal_distractor` | `confirm_target` | `reject_track` |
| r3-only | `f1a35b3f4ae816b2ce16e3fd` | `distractor_0009_03` | `gen_r21id_r3_w0009` | `front` | `distractor` | `confirm_target` | `reject_track` |
| r3-only | `f1b55da39507187f23381ff0` | `confuser_0009_00_00` | `gen_r21id_r3_w0009` | `front` | `proposal_distractor` | `confirm_target` | `reject_track` |
| 两者误收 | `f6282b7137572db3e9e95ac6` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `distractor` | `confirm_target` | `confirm_target` |
| 两者误收 | `ffe37f7a581ef2ec27ddbb99` | `confuser_0001_00_00` | `gen_r21id_r3_w0001` | `front` | `distractor` | `confirm_target` | `confirm_target` |

误收按 view 汇总如下；逐条的 target_id、world 和 sample_id 见上表。

| view | r3 误收 | r10 误收 |
|---|---:|---:|
| `front` | 51 | 28 |
| `oblique` | 20 | 15 |
| `edge_truncated` | 26 | 17 |
| `recovery` | 13 | 8 |

r10 的误收主要集中于 `front`（28）和 `edge_truncated`（17），其次为 `oblique`（15）和 `recovery`（8）。按 world 计，较多误收来自 `gen_r21id_r3_w0010`（15）、`gen_r21id_r3_w0002`（14）和 `gen_r21id_r3_w0009`（14）；所有分布均可由上表筛选复核。

## 文件校验值

| 文件 | SHA256 |
|---|---|
| `artifacts/r21id-r3/pipeline/r15-additions/test.jsonl` | `73014f4a969df7adde79dd10d3f4ea23d39a77e18b88d6a1ccbeb63db91fb6c1` |
| `artifacts/r21id-r10/evaluation/r3-four-view/identity-report.json` | `0dc5f75b56cd1528ca60835ec9e2a3e9163cbf82b007befeae4cb1e2a172fa73` |
| `artifacts/r21id-r10/evaluation/r3-four-view/identity-report.md` | `3d077e520f1d54ef5f18772de873c00d261ba4cb7c830086698720e42d634613` |
| `artifacts/r21id-r10/evaluation/r3-four-view/r21id-r3-predictions.json` | `6ee232620931d7a1e44a6d1dff5970f9db8119547bffca7bb9d6ffceb0f6260c` |
| `artifacts/r21id-r10/evaluation/r3-four-view/r21id-r10-predictions.json` | `b202ee4fedc857239eda06c93a5b22bdca380ec2d1aa513e876d336e867a691a` |
| `scripts/eval_r21id_identity.py` | `d84fb858e0dcb76949b73821e6d7e21f9eef5878e2a2b7d42603fb34add40783` |

## 验证与限制

这是文档审计，没有新增或运行 pytest，也没有启动模型服务；使用现有 evaluator 对只读冻结预测做离线重算和 sample_id 对齐。审计只定位统计失败所在的实例和视角，不从预测文本推断未记录的视觉原因。
