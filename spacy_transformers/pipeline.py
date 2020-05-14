from typing import List, Callable, Optional
from spacy.pipeline import Pipe
from spacy.language import component
from spacy.tokens import Doc
from spacy.vocab import Vocab
from spacy.gold import Example
from spacy.util import minibatch, eg2doc, link_vectors_to_models
from thinc.api import Model, set_dropout_rate

from .util import null_annotation_setter
from .util import FullTransformerBatch, TransformerData


@component("transformer", assigns=["doc._.trf_data"])
class Transformer(Pipe):
    """spaCy pipeline component to use transformer models.

    The component assigns the output of the transformer to the Doc's
    extension attributes. We also calculate an alignment between the word-piece
    tokens and the spaCy tokenization, so that we can use the last hidden states
    to set the doc.tensor attribute. When multiple word-piece tokens align to
    the same spaCy token, the spaCy token receives the sum of their values.
    """

    def __init__(
        self,
        vocab: Vocab,
        model: Model[List[Doc], FullTransformerBatch],
        annotation_setter: Callable = null_annotation_setter,
        *,
        max_batch_size: int = 8,
        **cfg,
    ):
        self.vocab = vocab
        self.model = model
        assert isinstance(self.model, Model)
        self.annotation_setter = annotation_setter
        self.cfg = dict(cfg)
        self.cfg["max_batch_size"] = max_batch_size
        self.listeners: List[TransformerListener] = []

    def create_listener(self):
        listener = TransformerListener(
            upstream_name="transformer", width=self.model.get_dim("nO")
        )
        self.listeners.append(listener)

    def add_listener(self, listener):
        self.listeners.append(listener)

    def find_listeners(self, model):
        for node in model.walk():
            if (
                isinstance(node, TransformerListener)
                and node.upstream_name == self.name
            ):
                self.add_listener(node)

    def __call__(self, doc):
        outputs = self.predict([doc])
        self.set_annotations([doc], outputs)
        return doc

    def pipe(self, stream, batch_size=128, n_threads=-1, as_example=False):
        batch_size = max(batch_size, self.cfg["max_batch_size"])
        for batch in minibatch(stream, batch_size):
            batch = list(batch)
            if as_example:
                docs = [eg2doc(doc) for doc in batch]
            else:
                docs = batch
            outputs = self.predict(docs)
            self.set_annotations(docs, outputs)
            yield from batch

    def predict(self, docs) -> FullTransformerBatch:
        activations = self.model.predict(docs)
        batch_id = TransformerListener.get_batch_id(docs)
        for listener in self.listeners:
            listener.receive(batch_id, activations.doc_data, None)
        return activations

    def set_annotations(self, docs: List[Doc], predictions: FullTransformerBatch):
        """Assign the extracted features to the Doc objects and overwrite the
        vector and similarity hooks.

        docs (iterable): A batch of `Doc` objects.
        activations (iterable): A batch of activations.
        """
        for doc, data in zip(docs, predictions.doc_data):
            doc._.trf_data = data
        self.annotation_setter(docs, predictions)

    def update(self, examples, drop=0.0, sgd=None, losses=None, set_annotations=False):
        """Update the model.
        examples (iterable): A batch of examples
        drop (float): The droput rate.
        sgd (callable): An optimizer.
        RETURNS (dict): Results from the update.
        """
        if losses is None:
            losses = {}
        examples = Example.to_example_objects(examples)
        docs = [eg.doc for eg in examples]
        if isinstance(docs, Doc):
            docs = [docs]
        set_dropout_rate(self.model, drop)
        trf_full, bp_trf_full = self.model.begin_update(docs)
        d_tensors = []

        losses.setdefault(self.name, 0.0)

        def accumulate_gradient(d_trf_datas: List[TransformerData]):
            """Accumulate tok2vec loss and gradient. This is passed as a callback
            to all but the last listener. Only the last one does the backprop.
            """
            nonlocal d_tensors
            for i, d_trf_data in enumerate(d_trf_datas):
                for d_tensor in d_trf_data.tensors:
                    losses[self.name] += float((d_tensor ** 2).sum())  # type: ignore
                if i >= len(d_tensors):
                    d_tensors.append(d_trf_data.tensors)
                else:
                    for j, d_tensor in enumerate(d_trf_data.tensors):
                        d_tensors[i][j] += d_tensor

        def backprop(d_trf_datas: List[TransformerData]):
            """Callback to actually do the backprop. Passed to last listener."""
            nonlocal d_tensors
            accumulate_gradient(d_trf_datas)
            d_trf_full = trf_full.unsplit_by_doc(d_tensors)
            d_docs = bp_trf_full(d_trf_full)
            if sgd is not None:
                self.model.finish_update(sgd)
            return d_docs

        batch_id = TransformerListener.get_batch_id(docs)
        for listener in self.listeners[:-1]:
            listener.receive(batch_id, trf_full.doc_data, accumulate_gradient)
        self.listeners[-1].receive(batch_id, trf_full.doc_data, backprop)
        if set_annotations:
            self.set_annotations(docs, trf_full)

    def get_loss(self, docs, golds, scores):
        pass

    def begin_training(
        self, get_examples=lambda: [], pipeline=None, sgd=None, **kwargs
    ):
        """Allocate models and pre-process training data

        get_examples (function): Function returning example training data.
        pipeline (list): The pipeline the model is part of.
        """
        docs = [Doc(Vocab(), words=["hello"])]
        self.model.initialize(X=docs)
        link_vectors_to_models(self.vocab)


class TransformerListener(Model):
    """A layer that gets fed its answers from an upstream connection,
    for instance from a component earlier in the pipeline.
    """

    name = "transformer-listener"

    _batch_id: Optional[int]
    _outputs: Optional[List[TransformerData]]
    _backprop: Optional[Callable[[List[TransformerData]], List[Doc]]]

    def __init__(self, upstream_name, width):
        Model.__init__(self, name=self.name, forward=forward, dims={"nO": width})
        self.upstream_name = upstream_name
        self._batch_id = None
        self._outputs = None
        self._backprop = None

    @classmethod
    def get_batch_id(cls, inputs: List[Doc]):
        return sum(sum(token.orth for token in doc) for doc in inputs)

    def receive(self, batch_id, outputs, backprop):
        self._batch_id = batch_id
        self._outputs = outputs
        self._backprop = backprop

    def verify_inputs(self, inputs):
        if self._batch_id is None and self._outputs is None:
            raise ValueError
        else:
            batch_id = self.get_batch_id(inputs)
            if batch_id != self._batch_id:
                raise ValueError(f"Mismatched IDs! {batch_id} vs {self._batch_id}")
            else:
                return True


def forward(model: TransformerListener, docs, is_train):
    if is_train:
        model.verify_inputs(docs)
        return model._outputs, model._backprop
    else:
        if len(docs) == 0:
            return [TransformerData.empty()], lambda d_data: docs
        else:
            return [doc._.trf_data for doc in docs], lambda d_data: docs
