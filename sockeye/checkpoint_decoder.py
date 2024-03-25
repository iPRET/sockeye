# Copyright 2017--2022 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not
# use this file except in compliance with the License. A copy of the License
# is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either
# express or implied. See the License for the specific language governing
# permissions and limitations under the License.

"""
Implements a thin wrapper around Translator to compute BLEU scores on (a sample of) validation data during training.
"""
import itertools
import logging
import os
import random
import time
from contextlib import ExitStack
from itertools import chain
from typing import Any, Dict, Optional, List

import torch

from . import constants as C
from . import evaluate
from . import inference
from . import model
from . import utils
from . import vocab

logger = logging.getLogger(__name__)


class CheckpointDecoder:
    """
    Decodes a (random sample of a) dataset using parameters at given checkpoint and computes BLEU against references.

    :param model_folder: The model folder where checkpoint decoder outputs will be written to.
    :param inputs: Path(s) to file containing input sentences (and their factors).
    :param references: Path to file containing references (and their factors).
    :param source_vocabs: The source vocabularies.
    :param target_vocabs: The target vocabularies.
    :param device: The device to use for decoding.
    :param model: The translation model.
    :param max_input_len: Maximum input length.
    :param batch_size: Batch size.
    :param beam_size: Size of the beam.
    :param nbest_size: Size of nbest lists.
    :param length_penalty_alpha: Alpha factor for the length penalty.
    :param length_penalty_beta: Beta factor for the length penalty.
    :param max_output_length_num_stds: Number of standard deviations as safety margin for maximum output length.
    :param ensemble_mode: Ensemble mode: linear or log_linear combination.
    :param sample_size: Maximum number of sentences to sample and decode. If <=0, all sentences are used.
    :param random_seed: Random seed for sampling. Default: 42.
    """

    def __init__(self,
                 model_folder: str,
                 inputs: List[str],
                 references: List[str],
                 source_vocabs: List[vocab.Vocab],
                 target_vocabs: List[vocab.Vocab],
                 model: model.SockeyeModel,
                 device: torch.device,
                 max_input_len: Optional[int] = None,
                 batch_size: int = 16,
                 beam_size: int = C.DEFAULT_BEAM_SIZE,
                 nbest_size: int = C.DEFAULT_NBEST_SIZE,
                 bucket_width_source: int = 10,
                 length_penalty_alpha: float = 1.0,
                 length_penalty_beta: float = 0.0,
                 max_output_length_num_stds: int = C.DEFAULT_NUM_STD_MAX_OUTPUT_LENGTH,
                 ensemble_mode: str = 'linear',
                 sample_size: int = -1,
                 random_seed: int = 42,
                 alignment_matrix: Optional[str] = None) -> None:
        self.max_input_len = max_input_len
        self.max_output_length_num_stds = max_output_length_num_stds
        self.ensemble_mode = ensemble_mode
        self.beam_size = beam_size
        self.nbest_size = nbest_size
        self.batch_size = batch_size
        self.bucket_width_source = bucket_width_source
        self.length_penalty_alpha = length_penalty_alpha
        self.length_penalty_beta = length_penalty_beta
        self.model = model

        with ExitStack() as exit_stack:
            inputs_fins = [exit_stack.enter_context(utils.smart_open(f)) for f in inputs]
            references_fins = [exit_stack.enter_context(utils.smart_open(f)) for f in references]

            inputs_sentences = [f.readlines() for f in inputs_fins]
            targets_sentences = [f.readlines() for f in references_fins]
            alignment_matrix_strings = exit_stack.enter_context(utils.smart_open(alignment_matrix)).readlines() \
                if alignment_matrix is not None else None

            utils.check_condition(all(len(l) == len(targets_sentences[0])
                                      for l in chain(inputs_sentences, targets_sentences)),
                                  "Sentences differ in length.")
            utils.check_condition(all(len(sentence.strip()) > 0 for sentence in targets_sentences[0]),
                                  "Empty target validation sentence.")
            if alignment_matrix is not None:
                utils.check_condition(len(alignment_matrix_strings) == len(inputs_sentences[0]),
                                      "Number of validation metadata lines differs from number of input lines.")

            if sample_size <= 0:
                sample_size = len(inputs_sentences[0])
            if sample_size < len(inputs_sentences[0]):
                sentences = parallel_subsample(
                    inputs_sentences + targets_sentences + ([alignment_matrix_strings]
                                                            if alignment_matrix is not None else []),
                    sample_size, random_seed)
                self.inputs_sentences = sentences[0:len(inputs_sentences)]
                self.targets_sentences = sentences[len(inputs_sentences):len(inputs_sentences) + len(targets_sentences)]
                self.alignment_matrix_strings = sentences[-1] if alignment_matrix is not None else None
            else:
                self.inputs_sentences, self.targets_sentences = inputs_sentences, targets_sentences
                self.alignment_matrix_strings = alignment_matrix_strings

            if sample_size < self.batch_size:
                self.batch_size = sample_size

        for factor_idx, factor in enumerate(self.inputs_sentences):
            write_to_file(factor, os.path.join(model_folder, C.DECODE_IN_NAME.format(factor=factor_idx)))
        for factor_idx, factor in enumerate(self.targets_sentences):
            write_to_file(factor, os.path.join(model_folder, C.DECODE_REF_NAME.format(factor=factor_idx)))
        if self.alignment_matrix_strings is not None:
            write_to_file(self.alignment_matrix_strings, os.path.join(model_folder, C.DECODE_ALIGNMENT_MATRIX_NAME))

        self.inputs_sentences = list(zip(*self.inputs_sentences))  # type: ignore

        scorer = inference.CandidateScorer(
            length_penalty_alpha=length_penalty_alpha,
            length_penalty_beta=length_penalty_beta,
            brevity_penalty_weight=0.0)

        self.translator = inference.Translator(
            batch_size=self.batch_size,
            device=device,
            ensemble_mode=self.ensemble_mode,
            scorer=scorer,
            beam_search_stop='all',
            nbest_size=self.nbest_size,
            models=[self.model],
            source_vocabs=source_vocabs,
            target_vocabs=target_vocabs,
            restrict_lexicon=None)

        logger.info("Created CheckpointDecoder(max_input_len=%d, beam_size=%d, num_sentences=%d)",
                    max_input_len if max_input_len is not None else -1, beam_size, len(self.targets_sentences[0]))

    def decode_and_evaluate(self, output_name: Optional[str] = None) -> Dict[str, float]:
        """
        Decodes data set and evaluates given a checkpoint.

        :param output_name: Filename to write translations to. If None, will not write outputs.
        :return: Mapping of metric names to scores.
        """

        # 1. Translate

        # Store original mode and set to eval mode in case the model is not yet
        # traced.
        original_mode = self.model.training
        self.model.eval()

        trans_wall_time = 0.0
        translations = []  # type: List[List[str]]
        with ExitStack() as exit_stack:
            outputs = [exit_stack.enter_context(utils.smart_open(output_name.format(factor=idx), 'w'))
                       if output_name is not None else None for idx in range(self.model.num_target_factors)]

            tic = time.time()
            trans_inputs = []  # type: List[inference.TranslatorInput]
            for i, (inputs, alignment_matrix) in enumerate(
                itertools.zip_longest(self.inputs_sentences,
                                      (self.alignment_matrix_strings if self.alignment_matrix_strings is not None else []))):
                trans_inputs.append(inference.make_input_from_multiple_strings(i, inputs,
                                                                               alignment_matrix_string=alignment_matrix))
            trans_outputs = self.translator.translate(trans_inputs)
            trans_wall_time = time.time() - tic
            for trans_input, trans_output in zip(trans_inputs, trans_outputs):
                output_strings = [trans_output.translation]
                if trans_output.factor_translations is not None and len(outputs) > 1:
                    output_strings += trans_output.factor_translations
                translations.append(output_strings)
                for output_string, output_file in zip(output_strings, outputs):
                    if output_file is not None:
                        print(output_string, file=output_file)
        avg_time = trans_wall_time / len(self.targets_sentences[0])
        translations = list(zip(*translations))  # type: ignore

        # Restore original model mode
        self.model.train(original_mode)

        # 2. Evaluate

        metrics = {C.BLEU: evaluate.raw_corpus_bleu(hypotheses=translations[0],
                                                    references=self.targets_sentences[0],
                                                    offset=evaluate.DEFAULT_OFFSET),
                   C.CHRF: evaluate.raw_corpus_chrf(hypotheses=translations[0],
                                                    references=self.targets_sentences[0]),
                   C.ROUGE1: evaluate.raw_corpus_rouge1(hypotheses=translations[0],
                                                        references=self.targets_sentences[0]),
                   C.ROUGE2: evaluate.raw_corpus_rouge2(hypotheses=translations[0],
                                                        references=self.targets_sentences[0]),
                   C.ROUGEL: evaluate.raw_corpus_rougel(hypotheses=translations[0],
                                                        references=self.targets_sentences[0]),
                   C.LENRATIO: evaluate.raw_corpus_length_ratio(hypotheses=translations[0],
                                                                references=self.targets_sentences[0]),
                   C.TER: evaluate.raw_corpus_ter(hypotheses=translations[0],
                                                  references=self.targets_sentences[0]),
                   C.AVG_TIME: avg_time,
                   C.DECODING_TIME: trans_wall_time}

        if len(translations) > 1:  # metrics for other target factors
            for i, _ in enumerate(translations[1:], 1):
                # only BLEU
                metrics.update(
                    {'f%d-%s' % (i, C.BLEU): evaluate.raw_corpus_bleu(hypotheses=translations[i],
                                                                      references=self.targets_sentences[i],
                                                                      offset=evaluate.DEFAULT_OFFSET)}
                )
        return metrics

    def warmup(self):
        """
        Translate a single sentence to warm up the model. Set the model to eval
        mode for tracing, translate the sentence, then set the model back to its
        original mode.
        """
        original_mode = self.model.training
        self.model.eval()
        one_sentence = [inference.make_input_from_multiple_strings(0, self.inputs_sentences[0],
                                                                   alignment_matrix_string=self.alignment_matrix_strings[0]
                                                                   if self.alignment_matrix_strings is not None else None)]
        _ = self.translator.translate(one_sentence)
        self.model.train(original_mode)


def parallel_subsample(parallel_sequences: List[List[Any]], sample_size: int, seed: int) -> List[Any]:
    # custom random number generator to guarantee the same samples across runs in order to be able to
    # compare metrics across independent runs
    random_gen = random.Random(seed)
    parallel_sample = list(zip(*random_gen.sample(list(zip(*parallel_sequences)), sample_size)))
    return parallel_sample


def write_to_file(data: List[str], fname: str):
    with utils.smart_open(fname, 'w') as f:
        for x in data:
            print(x.rstrip(), file=f)
