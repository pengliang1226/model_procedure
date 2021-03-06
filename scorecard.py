# encoding: utf-8
"""
@author: pengliang.zhao
@time: 2020/10/21 19:34
@file: scorecard.py
@desc: 
"""
import re

import pandas as pd
from sklearn.model_selection import train_test_split

from eval_metrics import ScoreStretch, calc_ks, calc_auc
from feature_binning import DecisionTreeBinner, QuantileBinner
from model_training import BasicTrainer
from feature_select import *
from util import *

if __name__ == '__main__':
    # 读取数据
    data = pd.read_csv(r'D:\workbook\jupyter notebook\金融风控\建模代码\data_pos.csv')

    ori_data = data.copy()
    ori_data.drop(columns='cus_num', inplace=True)
    drop_col = [v for v in ori_data.columns if re.match('flag_|score', v)]
    ori_data.drop(columns=drop_col, inplace=True)
    y = ori_data.pop('y')
    user_date = ori_data.pop('user_date')
    ori_data.insert(0, 'y', y)
    ori_data.insert(1, 'user_date', user_date)

    # 填充空值为-999
    ori_data.replace({'-111': -111, '-999': -999}, inplace=True)
    null_flag = {x: [-111, -999] for x in ori_data.columns[2:]}

    # 切分数据
    train_data = ori_data[~ori_data['user_date'].str.contains('2018-08')].copy()
    test_data = ori_data[ori_data['user_date'].str.contains('2018-08')].copy()

    # 根据缺失率，同值占比，唯一值占比进行筛选
    tmp = dtype_filter(train_data, list(train_data.columns[2:]))
    tmp = missing_filter(train_data, tmp, null_flag=null_flag)
    tmp = mode_filter(train_data, tmp, null_flag=null_flag)
    first_feats = unique_filter(train_data, tmp, null_flag=null_flag)

    # 获取变量属性类型
    features_type = {}
    for col in first_feats:
        col_data = ori_data[col]
        features_type[col] = get_attr_by_unique(col_data, null_value=null_flag[col])

    # ----------------------------------------------PSI筛选----------------------------------------------#
    QT = QuantileBinner(features_info=features_type, features_nan_value=null_flag, max_leaf_nodes=6)
    QT.fit(train_data, train_data['y'], first_feats)

    res = PSI_filter(train_data[train_data['user_date'] < '2018-04-30'],
                     train_data[train_data['user_date'] >= '2018-04-30'], bins_info=QT.features_bins,
                     feature_type=features_type)

    second_feats = list(res.keys())

    # ----------------------------------------------iv值筛选--------------------------------------------------#
    DT = DecisionTreeBinner(features_info=features_type, features_nan_value=null_flag, max_leaf_nodes=6)
    DT.fit(train_data, train_data['y'], second_feats)

    # 对IV值大于0.02变量调整分箱单调性
    tmp = [k for k, v in DT.features_iv.items() if v > 0.02]
    DT.binning_trim(train_data, train_data['y'], tmp)
    # 分箱区间数大于1且iv值大于0.02的变量
    tmp = {}
    for col in second_feats:
        iv = DT.features_iv[col]
        flag = DT.features_bins[col]['flag']
        bins_len = len(DT.features_bins[col]['bins'])
        if iv <= 0.02:
            continue
        if (flag == 1 and bins_len == 2) or (flag == 0 and bins_len == 1):
            continue
        tmp[col] = iv

    third_feats = [v[0] for v in sorted(tmp.items(), key=lambda x: x[1], reverse=True)]

    # -----------------------------------------------woe转码---------------------------------------------------#
    train_data = train_data.loc[:, ['y', 'user_date'] + third_feats]
    test_data = test_data.loc[:, ['y', 'user_date'] + third_feats]
    for col in third_feats:
        train_data[col] = woe_transform(train_data[col], features_type[col], DT.features_bins[col],
                                        DT.features_woes[col])
        test_data[col] = woe_transform(test_data[col], features_type[col], DT.features_bins[col], DT.features_woes[col])

    # ----------------------------------------------相关系数筛选----------------------------------------------#
    forth_feats = correlation_filter(train_data, third_feats)

    # ----------------------------------------------多重共线性筛选----------------------------------------------#
    fifth_feats = vif_filter(train_data, forth_feats, threshold=10)

    # ---------------------------------------------------剔除系数为负数的特征-----------------------------------------#
    six_feats = coef_forward_filter(train_data, train_data['y'], fifth_feats)

    # ------------------------------------------------显著性筛选------------------------------------------------------#
    seven_feats = logit_pvalue_forward_filter(train_data, train_data['y'], six_feats)

    # -----------------------------------------训练模型------------------------------------------#
    train_x, val_x, train_y, val_y = train_test_split(train_data[seven_feats], train_data['y'], test_size=0.2,
                                                      random_state=123)
    test_x, test_y = test_data[seven_feats], test_data['y']
    params = {
        # 默认参数
        "solver": 'liblinear',
        "multi_class": 'ovr',
        # 更新参数
        "max_iter": 100,
        "penalty": "l2",
        "C": 1.0,
        "random_state": 0
    }
    model = BasicTrainer(algorithm='lr', params=params)
    model.fit(train_x, train_y)
    train_pred = model.estimator.predict_proba(train_x)[:, -1]
    val_pred = model.estimator.predict_proba(val_x)[:, -1]
    test_pred = model.estimator.predict_proba(test_x)[:, -1]

    # 获取入模变量相关信息字典
    bins_info = {}
    for col in seven_feats:
        bins_info[col] = {}
        bins_info[col]['bins'] = DT.features_bins[col]['bins']
        bins_info[col]['woes'] = DT.features_woes[col]
        bins_info[col]['flag'] = DT.features_bins[col]['flag']
        bins_info[col]['type'] = DT.features_info[col]
    sc = ScoreStretch(S=ori_data['y'].sum() / ori_data.shape[0], pred=train_pred)
    sc.transform_pred_to_score(test_pred)
    sc.transform_data_to_score(test_x, model.estimator)

    val_ks = calc_ks(val_y, val_pred)
    test_ks = calc_ks(test_y, test_pred)
    val_auc = calc_auc(val_y, val_pred)
    test_auc = calc_auc(test_y, test_pred)
    print('验证集auc={}, ks={}'.format(val_auc, val_ks))
    print('测试集auc={}, ks={}'.format(test_auc, test_ks))
