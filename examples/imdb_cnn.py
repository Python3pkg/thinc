
import tqdm
import pickle

from thinc.i2v import StaticVectors, HashEmbed
from thinc.v2v import Model, SELU, ReLu, Maxout, Softmax, Affine
from thinc.t2t import ExtractWindow
from thinc.t2t import ParametricAttention
from thinc.t2v import Pooling, sum_pool, max_pool, mean_pool
from thinc.misc import BatchNorm as BN
from thinc.misc import LayerNorm as LN
from thinc.misc import Residual

from thinc.extra import datasets
from thinc.neural.util import to_categorical
from thinc.api import layerize, chain, concatenate, clone
from thinc.api import foreach, foreach_sentence, uniqued
from thinc.api import layerize, with_flatten, flatten_add_lengths, with_getitem
from thinc.api import FeatureExtracter
import spacy
from spacy.attrs import ORTH, LOWER, SHAPE, PREFIX, SUFFIX, ID

from thinc.neural.ops import CupyOps
from spacy.util import compounding


@layerize
def get_sents(docs, drop=0.):
    sents = [list(doc.sents) for doc in docs]
    return sents, None


def build_model(nr_class, width, depth, conv_depth, **kwargs):
    with Model.define_operators({'|': concatenate, '>>': chain, '**': clone}):
        embed = (
            (HashEmbed(width, 5000, column=1)
            | StaticVectors('spacy_pretrained_vectors', width, column=5)
            | HashEmbed(width//2, 750, column=2)
            | HashEmbed(width//2, 750, column=3)
            | HashEmbed(width//2, 750, column=4))
            >> LN(Maxout(width))
        )

        sent2vec = (
            flatten_add_lengths
            >> with_getitem(0,
                embed
                >> Residual(ExtractWindow(nW=1) >> LN(Maxout(width))) ** conv_depth
            )
            >> ParametricAttention(width)
            >> Pooling(sum_pool)
            >> Residual(LN(Maxout(width))) ** depth
        )

        model = (
            foreach(sent2vec, drop_factor=2.0)
            >> flatten_add_lengths
            >> ParametricAttention(width, hard=False)
            >> Pooling(sum_pool)
            >> Residual(LN(Maxout(width))) ** depth
            >> Softmax(nr_class)
        )
    model.lsuv = False
    return model


def main(use_gpu=True, nb_epoch=100):
    if use_gpu:
        Model.ops = CupyOps()
        Model.Ops = CupyOps
    train, test = datasets.imdb()
    print("Load data")
    train_X, train_y = list(zip(*train))
    test_X, test_y = list(zip(*test))
    train_y = Model.ops.asarray(to_categorical(train_y, nb_classes=2))
    test_y = Model.ops.asarray(to_categorical(test_y, nb_classes=2))
    
    nlp = spacy.load('en_vectors_web_lg')
    nlp.add_pipe(nlp.create_pipe('sentencizer'), first=True)

    preprocessor = FeatureExtracter([ORTH, LOWER, PREFIX, SUFFIX, SHAPE, ID])
    train_X = [preprocessor(list(doc.sents)) for doc in tqdm.tqdm(nlp.pipe(train_X))]
    test_X = [preprocessor(list(doc.sents)) for doc in tqdm.tqdm(nlp.pipe(test_X))]

    dev_X = train_X[-1000:]
    dev_y = train_y[-1000:]
    train_X = train_X[:-1000]
    train_y = train_y[:-1000]
    print("Parse data")
    n_sent = sum([len(list(sents)) for sents in train_X])
    print("%d sentences" % n_sent)

    model = build_model(2, width=128, conv_depth=2, depth=2,
                        train_X=train_X, train_y=train_y)
    with model.begin_training(train_X[:100], train_y[:100]) as (trainer, optimizer):
        epoch_loss = [0.]
        def report_progress():
            with model.use_params(optimizer.averages):
                print(epoch_loss[-1], model.evaluate(dev_X, dev_y), trainer.dropout)
            epoch_loss.append(0.)
        trainer.each_epoch.append(report_progress)
        batch_sizes = compounding(4, 1024, 1.001)
        trainer.dropout = 0.3
        trainer.batch_size = next(batch_sizes)
        trainer.dropout_decay = 0.0
        trainer.nb_epoch = nb_epoch
        optimizer.learn_rate = 0.001
        for X, y in trainer.iterate(train_X, train_y):
            yh, backprop = model.begin_update(X, drop=trainer.dropout)
            loss = ((yh-y)**2.).sum() / y.shape[0]
            backprop((yh-y)/yh.shape[0], optimizer)
            epoch_loss[-1] += loss
            trainer.batch_size = next(batch_sizes)
        with model.use_params(optimizer.averages):
            print('Avg dev.: %.3f' % model.evaluate(dev_X, dev_y))


if __name__ == '__main__':
    main()
