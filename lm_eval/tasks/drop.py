import json
import numpy as np
import re
import string
from best_download import download_file
from scipy.optimize import linear_sum_assignment
from lm_eval.base import Task, rf
from lm_eval.metrics import mean
from pathlib import Path
from zipfile import ZipFile

"""
Acknowledgement: This implementation is based on the official evaluation for `DROP`:
https://github.com/allenai/allennlp-reading-comprehension/blob/master/allennlp_rc/eval/drop_eval.py
"""

_ARTICLES = re.compile(r"\b(a|an|the)\b", re.UNICODE)

class DROP(Task):
    VERSION = 1
    DATASET_PATH = Path("data/drop")

    def download(self):
        if self.DATASET_PATH.exists():
            return
        Path.mkdir(self.DATASET_PATH, parents=True)
        url = "https://s3-us-west-2.amazonaws.com/allennlp/datasets/drop/drop_dataset.zip"
        checksum = "39d2278a29fd729de301b111a45f434c24834f40df8f4ff116d864589e3249d6"
        zip_path = self.DATASET_PATH / "drop_dataset.zip"
        download_file(url, local_file=str(zip_path), expected_checksum=checksum)
        with ZipFile(zip_path, "r") as zip:
            zip.extractall(self.DATASET_PATH)

    def has_training_docs(self):
        return True

    def has_validation_docs(self):
        return True

    def has_test_docs(self):
        return False

    def _load_docs(self, docs):
        for doc in docs:
            for qa in doc["qa_pairs"]:
                yield {
                    "id": qa["query_id"],
                    "passage": doc["passage"],
                    "question": qa["question"],
                    "answers": self.get_answers(qa),
                }

    @classmethod
    def get_answers(cls, qa):
        answers = []
        answers_set = set()

        candidates = [qa["answer"]] + qa.get("validated_answers", [])
        for candidate in candidates:
            answer = cls.parse_answer(candidate)
            if answer in answers_set:
                continue
            answers_set.add(answer)
            answers.append(answer)

        return answers

    @classmethod
    def parse_answer(cls, answer):
        # NOTE: Everything is returned as a tuple for uniformity and hashability.
        if answer["number"] != "":
            return (str(answer["number"]),)
        if answer["spans"] != []:
            return tuple(answer["spans"])
        return (" ".join([answer["date"]["day"],
                          answer["date"]["month"],
                          answer["date"]["year"]]).strip(),)

    def training_docs(self):
        docs = json.load(open(self.DATASET_PATH / "drop_dataset" / "drop_dataset_train.json"))
        return self._load_docs([docs[k] for k in docs.keys()])

    def validation_docs(self):
        docs = json.load(open(self.DATASET_PATH / "drop_dataset" / "drop_dataset_dev.json"))
        return self._load_docs([docs[k] for k in docs.keys()])

    def doc_to_text(self, doc):
        return f"Passage: {doc['passage']}\nQuestion: {doc['question']}\nAnswer:"

    def should_decontaminate(self):
        return True

    def doc_to_decontamination_query(self, doc):
        return doc['passage'] + " " + doc['question']

    def doc_to_target(self, doc):
        return " " + ", ".join(doc["answers"][0])

    def construct_requests(self, doc, ctx):
        """Uses RequestFactory to construct Requests and returns an iterable of
        Requests which will be sent to the LM.

        :param doc:
            The document as returned from training_docs, validation_docs, or test_docs.
        :param ctx: str
            The context string, generated by fewshot_context. This includes the natural
            language description, as well as the few shot examples, and the question
            part of the document for `doc`.
        """
        conts = [rf.greedy_until(ctx, ["."])]
        return conts

    def process_results(self, doc, results):
        """Take a single document and the LM results and evaluates, returning a
        dict where keys are the names of submetrics and values are the values of
        the metric for that one document

        :param doc:
            The document as returned from training_docs, validation_docs, or test_docs.
        :param results:
            The results of the requests created in construct_requests.
        """
        preds, golds = results, doc["answers"]
        max_em = 0
        max_f1 = 0
        for gold_answer in golds:
            exact_match, f1_score = self.get_metrics(preds, gold_answer)
            if gold_answer[0].strip():
                max_em = max(max_em, exact_match)
                max_f1 = max(max_f1, f1_score)
        return {
            "em": max_em,
            "f1": max_f1
        }

    def get_metrics(self, predicted, gold):
        """
        Takes a predicted answer and a gold answer (that are both either a string or a list of
        strings), and returns exact match and the DROP F1 metric for the prediction.  If you are
        writing a script for evaluating objects in memory (say, the output of predictions during
        validation, or while training), this is the function you want to call, after using
        :func:`answer_json_to_strings` when reading the gold answer from the released data file.
        """
        predicted_bags = self._answer_to_bags(predicted)
        gold_bags = self._answer_to_bags(gold)

        if set(predicted_bags[0]) == set(gold_bags[0]) and len(predicted_bags[0]) == len(gold_bags[0]):
            exact_match = 1.0
        else:
            exact_match = 0.0

        f1_per_bag = self._align_bags(predicted_bags[1], gold_bags[1])
        f1 = np.mean(f1_per_bag)
        f1 = round(f1, 2)
        return exact_match, f1

    def _answer_to_bags(self, answer):
        if isinstance(answer, (list, tuple)):
            raw_spans = answer
        else:
            raw_spans = [answer]
        normalized_spans = []
        token_bags = []
        for raw_span in raw_spans:
            normalized_span = self._normalize(raw_span)
            normalized_spans.append(normalized_span)
            token_bags.append(set(normalized_span.split()))
        return normalized_spans, token_bags

    def _align_bags(self, predicted, gold):
        """
        Takes gold and predicted answer sets and first finds the optimal 1-1 alignment
        between them and gets maximum metric values over all the answers.
        """
        scores = np.zeros([len(gold), len(predicted)])
        for gold_index, gold_item in enumerate(gold):
            for pred_index, pred_item in enumerate(predicted):
                if self._match_numbers_if_present(gold_item, pred_item):
                    scores[gold_index, pred_index] = self._compute_f1(pred_item, gold_item)
        row_ind, col_ind = linear_sum_assignment(-scores)

        max_scores = np.zeros([max(len(gold), len(predicted))])
        for row, column in zip(row_ind, col_ind):
            max_scores[row] = max(max_scores[row], scores[row, column])
        return max_scores

    def _compute_f1(self, predicted_bag, gold_bag):
        intersection = len(gold_bag.intersection(predicted_bag))
        if not predicted_bag:
            precision = 1.0
        else:
            precision = intersection / float(len(predicted_bag))
        if not gold_bag:
            recall = 1.0
        else:
            recall = intersection / float(len(gold_bag))
        f1 = (
            (2 * precision * recall) / (precision + recall)
            if not (precision == 0.0 and recall == 0.0)
            else 0.0
        )
        return f1

    def _match_numbers_if_present(self, gold_bag, predicted_bag):
        gold_numbers = set()
        predicted_numbers = set()
        for word in gold_bag:
            if self._is_number(word):
                gold_numbers.add(word)
        for word in predicted_bag:
            if self._is_number(word):
                predicted_numbers.add(word)
        if (not gold_numbers) or gold_numbers.intersection(predicted_numbers):
            return True
        return False

    def _is_number(self, text):
        try:
            float(text)
            return True
        except ValueError:
            return False

    def _remove_articles(self, text):
        return _ARTICLES.sub(" ", text)

    def _white_space_fix(self, text):
        return " ".join(text.split())

    def _remove_punc(self, text):
        exclude = set(string.punctuation)
        if not self._is_number(text):
            return "".join(ch for ch in text if ch not in exclude)
        else:
            return text

    def _fix_number(self, text):
        return str(float(text)) if self._is_number(text) else text

    def _tokenize(self, text):
        return re.split(" |-", text)

    def _normalize(self, answer):
        tokens = [
            self._white_space_fix(self._remove_articles(self._fix_number(self._remove_punc(token.lower()))))
            for token in self._tokenize(answer)
        ]
        tokens = [token for token in tokens if token.strip()]
        normalized = " ".join(tokens).strip()
        return normalized

    def aggregation(self):
        """
        :returns: {str: [float] -> float}
            A dictionary where keys are the names of submetrics and values are
            functions that aggregate a list of metrics
        """
        return {
            "em": mean,
            "f1": mean
        }

    def higher_is_better(self):
        """
        :returns: {str: bool}
            A dictionary where keys are the names of submetrics and values are
            whether a higher value of the submetric is better
        """
        return {
            "em": True,
            "f1": True
        }
