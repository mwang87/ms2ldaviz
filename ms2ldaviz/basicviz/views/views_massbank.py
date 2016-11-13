import csv
import math

import json
import jsonpickle
import networkx as nx
import numpy as np
import datetime

import requests
import pprint

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, HttpResponseRedirect, Http404
from django.shortcuts import render
from networkx.readwrite import json_graph
from scipy.stats import ttest_ind
from sklearn.decomposition import PCA
from numpy import interp

from basicviz.constants import AVAILABLE_OPTIONS, DEFAULT_MASSBANK_AUTHORS, DEFAULT_MASSBANK_SPLASH, \
    DEFAULT_AC_INSTRUMENT, DEFAULT_AC_INSTRUMENT_TYPE, DEFAULT_LICENSE, DEFAULT_IONISATION
from basicviz.forms import Mass2MotifMetadataForm, Mass2MotifMassbankForm, DocFilterForm, ValidationForm, VizForm, \
    UserForm, TopicScoringForm, AlphaCorrelationForm, SystemOptionsForm, AlphaDEForm
from basicviz.models import Feature, Experiment, Document, FeatureInstance, DocumentMass2Motif, \
    FeatureMass2MotifInstance, Mass2Motif, Mass2MotifInstance, VizOptions, UserExperiment, ExtraUsers, \
    MultiFileExperiment, MultiLink, Alpha, AlphaCorrOptions, SystemOptions


def get_description(motif):
    exp_desc = motif.experiment.description
    if exp_desc is None:
        # look up in multi-file experiment
        links = MultiLink.objects.filter(experiment=motif.experiment)
        for link in links:
            mfe = link.multifileexperiment
            if mfe.description is not None:
                return mfe.description  # found a multi-file descrption
        return None  # found nothing
    else:
        return exp_desc  # found single-file experiment description


def get_massbank_form(motif, motif_features, mf_id=None):
    motif_id = motif.id

    # retrieve existing massbank dictionary for this motif or initialise a default one
    if motif.massbank_dict is not None:
        mb_dict = motif.massbank_dict
        is_new = False
    else:
        data = {'motif_id': motif_id}
        mb_dict = get_massbank_dict(data, motif, motif_features, 0)
        is_new = True

    print 'is_new', is_new
    print 'mb_dict', mb_dict

    # set to another form used when generating the massbank record
    massbank_form = Mass2MotifMassbankForm(initial={
        'motif_id': motif_id,
        'accession': mb_dict['accession'],
        'authors': mb_dict['authors'],
        'comments': '\n'.join(mb_dict['comments']),
        'ch_name': '\n'.join(mb_dict['ch_name']),
        'ch_compound_class': mb_dict['ch_compound_class'],
        'ch_formula': mb_dict['ch_formula'],
        'ch_exact_mass': mb_dict['ch_exact_mass'],
        'ch_smiles': mb_dict['ch_smiles'],
        'ch_iupac': mb_dict['ch_iupac'],
        'ch_link': '\n'.join(mb_dict['ch_link']),
        'ac_instrument': mb_dict['ac_instrument'],
        'ac_instrument_type': mb_dict['ac_instrument_type'],
        'ac_mass_spectrometry_ion_mode': mb_dict['ac_mass_spectrometry_ion_mode'],
        'min_rel_int': 100 if is_new else mb_dict.get('min_rel_int', 100),
        'mf_id': mf_id if mf_id is not None else ''
    })
    return massbank_form

def get_massbank_dict(data, motif, motif_features, min_rel_int):
    default_accession = 'GP%06d' % int(data['motif_id'])
    accession = data.get('accession', default_accession)
    ms_type = 'MS2'

    if 'ac_mass_spectrometry_ion_mode' in data:
        ion_mode = data['ac_mass_spectrometry_ion_mode']
    else:
        # attempt to auto-detect from the experiment description
        exp_desc = get_description(motif)
        if exp_desc is not None:
            exp_desc = exp_desc.upper()
            ion_mode = 'POSITIVE' if 'POS' in exp_desc else 'NEGATIVE' if 'NEG' in exp_desc else 'Unknown'
        else:
            ion_mode = 'Unknown'

    # select the fragment/loss features to include
    peak_list = []
    for m2m in motif_features:
        tokens = m2m.feature.name.split('_')
        f_type = tokens[0]  # 'loss' or 'fragment'
        mz = float(tokens[1])
        if f_type == 'loss':  # represent neutral loss as negative m/z value
            mz = -mz
        abs_intensity = m2m.probability
        rel_intensity = m2m.probability
        row = (mz, abs_intensity, rel_intensity)
        peak_list.append(row)

    # this is [m/z, absolute intensity, relative intensity]
    peaks = np.array(peak_list)

    # sort by first (m/z) column
    mz = peaks[:, 0]
    peaks = peaks[mz.argsort()]

    # the probabilities * scale_fact are set to be the absolute intensities,
    # while the relative intensities are scaled from 1 ... 999 (from the manual)??
    scale_fact = 1000
    rel_range = [1, 999]
    abs_intensities = peaks[:, 1]
    min_prob = np.min(abs_intensities)
    max_prob = np.max(abs_intensities)
    rel_intensities = interp(abs_intensities, [min_prob, max_prob], rel_range)
    abs_intensities *= scale_fact  # do this only after computing the rel. intensities
    peaks[:, 2] = rel_intensities
    peaks[:, 1] = abs_intensities

    # filter features by the minimum relative intensity specified by the user
    pos = np.where(rel_intensities > min_rel_int)[0]
    peaks = peaks[pos, :]
    hash = get_splash(peaks)

    ch_names = data.get('ch_name', motif.annotation)
    if ch_names is None:
        ch_names = ['']
    else:
        ch_names = ch_names.splitlines()  # convert from string with \n into list

    comments = data.get('comments', '').splitlines()
    ch_exact_mass = data.get('ch_exact_mass', '0')
    ch_links = data.get('ch_link', '').splitlines()

    massbank_dict = {}
    massbank_dict['accession'] = accession
    massbank_dict['record_date'] = datetime.date.today().strftime('%Y.%m.%d')
    massbank_dict['authors'] = data.get('authors', DEFAULT_MASSBANK_AUTHORS)
    massbank_dict['license'] = DEFAULT_LICENSE
    massbank_dict['ch_name'] = ch_names
    massbank_dict['ac_instrument'] = data.get('ac_instrument', DEFAULT_AC_INSTRUMENT)
    massbank_dict['ac_instrument_type'] = data.get('ac_instrument_type', DEFAULT_AC_INSTRUMENT_TYPE)
    massbank_dict['ms_type'] = ms_type
    massbank_dict['comments'] = comments
    massbank_dict['ch_link'] = ch_links
    massbank_dict['ac_mass_spectrometry_ion_mode'] = ion_mode
    massbank_dict['ac_ionisation'] = DEFAULT_IONISATION
    massbank_dict['hash'] = hash
    massbank_dict['peaks'] = peaks

    tokens = [
        massbank_dict['ch_name'][0],
        massbank_dict['ac_instrument_type'],
        massbank_dict['ms_type']
    ]
    massbank_dict['record_title'] = ';'.join(tokens)

    # everything else, just copy
    to_copy = ['ch_compound_class', 'ch_formula', 'ch_smiles', 'ch_iupac', 'ch_exact_mass']
    for key in to_copy:
        massbank_dict[key] = data.get(key, '')
    massbank_dict['min_rel_int'] = min_rel_int

    return massbank_dict


def get_splash(peaks):
    # get splash hash of the spectra
    splash_data = {'ions': [], 'type': 'MS'}
    for peak in peaks:
        mz = '%.4f' % peak[0]
        abs_intensity = '%.3f' % peak[1]
        rel_intensity = '%d' % peak[2]
        if peak[0] > 0:  # excluding negative m/z values for now !!
            ion = {'mass': mz, 'intensity': abs_intensity}
            splash_data['ions'].append(ion)
    splash_data = json.dumps(splash_data)

    url = DEFAULT_MASSBANK_SPLASH
    print splash_data, url
    headers = {'Content-type': 'application/json'}
    response = requests.post(url, headers=headers, data=splash_data)
    hash = response.text
    return hash


def get_massbank_str(massbank_dict):
    print 'keys'
    for key in massbank_dict.keys():
        print '-', key

    output = []
    output.append('ACCESSION: %s' % massbank_dict['accession'])
    output.append('RECORD TITLE: %s' % massbank_dict['record_title'])
    output.append('DATE: %s' % massbank_dict['record_date'])
    output.append('AUTHORS: %s' % massbank_dict['authors'])
    output.append('LICENSE: %s' % massbank_dict['license'])
    for comment in massbank_dict['comments']:
        output.append('COMMENT: %s' % comment)
    for name in massbank_dict['ch_name']:
        output.append('CH$NAME: %s' % name)
    output.append('CH$COMPOUND_CLASS: %s' % massbank_dict['ch_compound_class'])
    output.append('CH$FORMULA: %s' % massbank_dict['ch_formula'])
    output.append('CH$EXACT_MASS: %s' % massbank_dict['ch_exact_mass'])
    output.append('CH$SMILES: %s' % massbank_dict['ch_smiles'])
    output.append('CH$IUPAC: %s' % massbank_dict['ch_iupac'])
    for link in massbank_dict['ch_link']:
        output.append('CH$LINK: %s' % link)

    output.append('AC$INSTRUMENT: %s' % massbank_dict['ac_instrument'])
    output.append('AC$INSTRUMENT_TYPE: %s' % massbank_dict['ac_instrument_type'])
    output.append('AC$MASS_SPECTROMETRY: MS_TYPE %s' % massbank_dict['ms_type'])
    output.append('AC$MASS_SPECTROMETRY: ION_MODE %s' % massbank_dict['ac_mass_spectrometry_ion_mode'])
    output.append('AC$MASS_SPECTROMETRY: IONIZATION %s' % massbank_dict['ac_ionisation'])

    peaks = massbank_dict['peaks']
    output.append('PK$SPLASH: %s' % massbank_dict['hash'])
    output.append('PK$NUM_PEAK: %d' % len(peaks))
    output.append('PK$PEAK: m/z int. rel.int.')
    for peak in peaks:
        mz = '%.4f' % peak[0]
        abs_intensity = '%.4f' % peak[1]
        rel_intensity = '%d' % peak[2]
        output.append('%s %s %s' % (mz, abs_intensity, rel_intensity))

    output.append('//')
    output_str = '\n'.join(output)
    return output_str


def generate_massbank(request):
    if request.method == 'POST':

        # populate from post request
        data = {}
        keys = [
            'motif_id', 'accession', 'authors', 'comments',
            'ch_name', 'ch_compound_class', 'ch_formula', 'ch_exact_mass',
            'ch_smiles', 'ch_iupac', 'ch_link',
            'ac_instrument', 'ac_instrument_type', 'ac_mass_spectrometry_ion_mode',
            'min_rel_int'
        ]
        for key in keys:
            data[key] = request.POST.get(key)

        motif_id = data['motif_id']
        min_rel_int = int(data['min_rel_int'])

        # get the data in dictionary form
        motif = Mass2Motif.objects.get(id=motif_id)
        motif_features = Mass2MotifInstance.objects.filter(mass2motif=motif).order_by('-probability')
        mb_dict = get_massbank_dict(data, motif, motif_features, min_rel_int)

        # convert to string and add to the dictionary
        mb_string = get_massbank_str(mb_dict)
        del (mb_dict['peaks'])  # got error if we jsonpickle this numpy array .. ?
        mb_dict['massbank_record'] = mb_string

        # decode the metadata first, add the massbank field, then encode it back
        md = jsonpickle.decode(motif.metadata)
        md['massbank'] = mb_dict
        motif.metadata = jsonpickle.encode(md)
        motif.save()

        response_data = {}
        response_data['status'] = 'Massbank record has been generated. Please copy.'
        response_data['massbank_str'] = mb_string
        return HttpResponse(
            json.dumps(response_data),
            content_type="application/json"
        )

    else:
        raise NotImplementedError


def generate_massbank_multi_m2m(request):
    if request.method == 'POST':

        # populate from post request
        data = {}
        keys = [
            'mf_id', 'motif_id', 'accession', 'authors', 'comments',
            'ch_name', 'ch_compound_class', 'ch_formula', 'ch_exact_mass',
            'ch_smiles', 'ch_iupac', 'ch_link',
            'ac_instrument', 'ac_instrument_type', 'ac_mass_spectrometry_ion_mode',
            'min_rel_int'
        ]
        for key in keys:
            data[key] = request.POST.get(key)

        mf_id = data['mf_id']
        first_motif_id = data['motif_id']
        min_rel_int = int(data['min_rel_int'])

        first_m2m = Mass2Motif.objects.get(id=first_motif_id)
        mfe = MultiFileExperiment.objects.get(id=mf_id)
        links = MultiLink.objects.filter(multifileexperiment=mfe).order_by('experiment__name')
        individuals = [l.experiment for l in links if l.experiment.status == 'all loaded']

        for individual in individuals:
            motif = Mass2Motif.objects.get(name=first_m2m.name, experiment=individual)

            # get the data in dictionary form
            motif_features = Mass2MotifInstance.objects.filter(mass2motif=motif).order_by('-probability')
            mb_dict = get_massbank_dict(data, motif, motif_features, min_rel_int)

            # convert to string and add to the dictionary
            mb_string = get_massbank_str(mb_dict)
            del (mb_dict['peaks'])  # got error if we jsonpickle this numpy array .. ?
            mb_dict['massbank_record'] = mb_string

            # decode the metadata first, add the massbank field, then encode it back
            md = jsonpickle.decode(motif.metadata)
            md['massbank'] = mb_dict
            motif.metadata = jsonpickle.encode(md)
            motif.save()

        response_data = {}
        response_data['status'] = 'Massbank record has been generated. Please copy.'
        response_data['massbank_str'] = mb_string
        return HttpResponse(
            json.dumps(response_data),
            content_type="application/json"
        )

    else:
        raise NotImplementedError

