#!/usr/bin/env python3
# -*- coding: utf8 -*-
import pandas as pd
import numpy as np
from flask import Flask, jsonify, request
from schema import Schema, And, Or, SchemaError

import pickle

from server import app

MODEL_PATH = 'server/models/sentence_pipe_mae1.776_2020-08-22_00h39m54s.pkl'
with open(MODEL_PATH, 'rb') as f:
    model = pickle.load(f)


PREDICT_SCHEMA = Schema({
    'CHARGE_COUNT': int,
    'CHARGE_DISPOSITION': And(str, len),
    'OFFENSE_CATEGORY': And(str, len),
    'PRIMARY_CHARGE_FLAG': bool,
    'DISPOSITION_CHARGED_OFFENSE_TITLE': And(str, len),
    'DISPOSITION_CHARGED_CLASS': And(str, len),
    'SENTENCE_JUDGE': And(str, len),
    'SENTENCE_PHASE': And(str, len),
    'COMMITMENT_TERM': Or(And(str,len), int, float),
    'COMMITMENT_UNIT': And(str, len),
    'LENGTH_OF_CASE_in_Days': Or(float, int),
    'AGE_AT_INCIDENT': Or(float, int),
    'RACE': And(str, len),
    'GENDER': And(str, len),
    'INCIDENT_CITY': Or(And(str, len), None),
    'LAW_ENFORCEMENT_AGENCY': And(str, len),
    'LAW_ENFORCEMENT_UNIT': Or(And(str, len), None),
    'SENTENCE_TYPE': And(str, len)
})

# PREDICT_KEYS = [
#     'OFFENSE_CATEGORY',
#     'PRIMARY_CHARGE_FLAG',
#     'DISPOSITION_CHARGED_OFFENSE_TITLE',
#     'CHARGE_COUNT',
#     'DISPOSITION_CHARGED_CLASS',
#     'CHARGE_DISPOSITION',
#     'SENTENCE_JUDGE',
#     'SENTENCE_PHASE',
#     'AGE_AT_INCIDENT',
#     'GENDER',
#     'LAW_ENFORCEMENT_AGENCY'
# ]


@app.route('/predict', methods=['POST'])
def predict():
    # validate input json
    try:
        data = PREDICT_SCHEMA.validate(request.json)
    except SchemaError as error:
        return jsonify(message=str(error)), 404
    
    # create df and clean data
    data = pd.DataFrame({k: [v] for k,v in data.items()})
    data = clean_data(data)
    
    # return explantation of why data is invalid
    if isinstance(data, str):
        return jsonify(message='DATA ERROR: '+data), 404

    # Ensure that the data is in the correct order for the model
    # model[0] is a sklearn ColumnTransformer obj
    orig_cols = model[0]._df_columns
    data = data[orig_cols]

    discrepancy = estimate_discrepancy(model, data)

    return jsonify(
        years_of_racial_bias_sentencing_discrepency = round(float(discrepancy[0]),3)
    )



def clean_data(data):
    '''
    Prepare Cook County Sentencing data for predictive model.
    Accepts multiple rows of data in pandas dataframe format.

    Params:
        data: (pd.DataFrame) Even if only one example/row is given, data is 
        expected in 2-d data frame format.

    Returns: Cleaned df, if valid rows remain after cleaning. If no valid rows 
        remain after cleaning a str message explaining what cleaning step 
        removed the last valid row is returned.


    TODO: restructure as a series of transformer objects that can be fit.
    '''
    #### Exclude non-prison setences ####
    # filter to only prison sentences (no jail or probation, etc...)
    data = data.loc[data['SENTENCE_TYPE']=='Prison', :]
    data = data.drop('SENTENCE_TYPE', axis=1)

    if data.shape[0]==0:
        return 'INVALID: No Prison sentences found'

    #### standardize race category names ####
    # NB: biracial was just 8 people out of 120k in original data set
    standard_race_map = {'Black': 'Black',
    'White': 'White',
    'HISPANIC': 'HISPANIC',
    'White [Hispanic or Latino]': 'HISPANIC',
    'White/Black [Hispanic or Latino]': 'HISPANIC',
    'ASIAN': 'Asian',
    'Asian': 'Asian',
    'American Indian': 'American Indian',
    'Unknown': 'Unknown',
    'Biracial': 'Black'}

    data['RACE'] = data['RACE'].map(standard_race_map)
    # we can't compare racial outcomes if race is not known
    data = data.loc[data['RACE']!='Unknown', :]
    # drop examples with races not not included in standard_race_map.keys()
    data = data.loc[data['RACE'].notnull(), :]

    if data.shape[0]==0:
        return 'INVALID: No valid race values found'


    #### standardize gender categories ####
    mask = ~data['GENDER'].isin(['Male', 'Female'])
    data.loc[mask, 'GENDER'] = 'Unknown'

    if data.shape[0]==0:
        return 'INVALID: No valid gender values found'

    #### normalize commitment term to year units ####
    # convert from object dtype
    data['COMMITMENT_TERM'] = data['COMMITMENT_TERM'].astype(float)

    # filter out examples with non-standard commitment term units
    commitment_term_units = ['Year(s)', 'Months', 'Natural Life', 'Days']
    mask = data['COMMITMENT_UNIT'].isin(commitment_term_units)
    data = data.loc[mask, :]

    # normalize commitment term to year units
    term_divisors = {'Year(s)': 1, 'Months':12, 'Days': 365}
    # fill rows where unit is natural life with divsor==1
    divisor_col = data['COMMITMENT_UNIT'].map(term_divisors).fillna(1)
    data['COMMITMENT_TERM'] = data['COMMITMENT_TERM']/divisor_col

    # define natural life commitment term in years as the difference between the 
    # median age of the indviduals committed to natural life terms at the time of 
    # their offence and the us life expectancy
    age_when_committed = data.loc[data['COMMITMENT_UNIT']=='Natural Life', 'AGE_AT_INCIDENT'].median()
    natural_life_years = 78 - age_when_committed  # 78 is US life expectancy
    # replace any value for commitment term where natural life is the unit to
    # the estimated year equivalent
    mask = data['COMMITMENT_UNIT']=='Natural Life'
    data.loc[mask,'COMMITMENT_TERM'] = natural_life_years

    data = data.drop('COMMITMENT_UNIT',axis=1)

    if data.shape[0]==0:
        return 'INVALID: No valid commitment term units found'



    #### drop variables and examples with NULLS ####
    # drop cols that had more than 5
    nan_cols = ['LENGTH_OF_CASE_in_Days', 'INCIDENT_CITY', 'LAW_ENFORCEMENT_UNIT']
    data = data.drop(nan_cols,axis=1)
    # drop examples with any nan!
    data = data.dropna(axis=0)

    if data.shape[0]==0:
        return 'INVALID: No null-free examples found'


    #### reduce cardinality of high cardinality categories ####
    top_ns = {'OFFENSE_CATEGORY':25, 'DISPOSITION_CHARGED_OFFENSE_TITLE': 40,
    'LAW_ENFORCEMENT_AGENCY': 20, 'SENTENCE_JUDGE': 73}
    # consolidate infrequent categories
    for name, n in top_ns.items():
        combine_cats = data[name].value_counts()[n:].index
        mask = data[name].isin(combine_cats)
        data.loc[mask, name] = 'misc_other'


    #### Clip range of COMMITMENT_TERM ####
    # clip any all value above to 110 years 
    data['COMMITMENT_TERM'] = data['COMMITMENT_TERM'].clip(upper=110)


    # Set correct column order
    # required by the sklearn ColumnTransformer used in the predict pipeline
    # predict_cols = ['OFFENSE_CATEGORY', 'PRIMARY_CHARGE_FLAG',
    #    'DISPOSITION_CHARGED_OFFENSE_TITLE', 'CHARGE_COUNT',
    #    'DISPOSITION_CHARGED_CLASS', 'CHARGE_DISPOSITION', 'SENTENCE_JUDGE',
    #    'SENTENCE_PHASE', 'COMMITMENT_TERM', 'AGE_AT_INCIDENT', 'RACE',
    #    'GENDER', 'LAW_ENFORCEMENT_AGENCY']
    # # assert all predict cols are same as cols in data
    # assert len(np.intersect1d(predict_cols, data.columns)) == len(data.columns)
    # data = data[predict_cols]
    
    return data



def make_counterfactual(data):
    '''Take data and switch race variable to "opposite" value'''
    # white --> black
    # non-white --> white
    race_counterfactual_map =\
    {'Black': 'White',
    'White': 'Black',
    'HISPANIC': 'White',
    'Asian': 'White',
    'American Indian': 'White'}

    data_counterfactual = data.copy()
    data_counterfactual['RACE'] = data['RACE'].map(race_counterfactual_map)
    return data_counterfactual



def estimate_discrepancy(model, data):
    '''estimate discrepancy in setnece length if race was switched'''
    diff = model.predict(make_counterfactual(data)) - model.predict(data)
    return diff