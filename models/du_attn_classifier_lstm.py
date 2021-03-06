import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.optim import Adam

from utils import *

SOS_SYMBOL = "<SOS>"

device = 'cuda' if torch.cuda.is_available() else 'cpu'

class RawEmbeddingLayer(nn.Module):
    def __init__(self, input_dim, full_dict_size, embedding_dropout_rate):
        super(RawEmbeddingLayer, self).__init__()
        self.dropout = nn.Dropout(embedding_dropout_rate)
        self.word_embedding = nn.Embedding(full_dict_size, input_dim)

        # I added this
        self.dict_size = full_dict_size

    # Takes either a non-batched input [sent len x input_dim] or a batched input
    # [batch size x sent len x input dim]
    def forward(self, input):
        embedded_words = self.word_embedding(input)
        final_embeddings = self.dropout(embedded_words)
        return final_embeddings


class PretrainedEmbeddingLayer(nn.Module):
    # Parameters: dimension of the word embeddings, number of words, and the dropout rate to apply
    # (0.2 is often a reasonable value)
    def __init__(self, word_vectors, embedding_dropout_rate, type="pretrained"):
        super(PretrainedEmbeddingLayer, self).__init__()
        self.dropout = nn.Dropout(embedding_dropout_rate)
        if type == "pretrained":
            self.word_embedding = nn.Embedding.from_pretrained(torch.from_numpy(word_vectors.vectors).float(), False)
            self.word_vectors = word_vectors

    # Takes either a non-batched input [sent len x input_dim] or a batched input
    # [batch size x sent len x input dim]
    def forward(self, input):
        try:
            embedded_words = self.word_embedding(input)
        except:
            print(len(self.word_vectors.word_indexer))
            for i in input:
                for j in i:
                    print(j)
        final_embeddings = self.dropout(embedded_words)
        return final_embeddings

# Spooky Dataset
#
# Average accuracy: 4527/5827 = .777, 8 epochs with 70/30 split
# Average accuracy: 4533/5827 = .778, 8 epochs with 70/30 split, 1-grams of POS tags
# Average accuracy: 4444/5827: 0.763, 8 epochs with 70/30 split, 2-grams of POS tags
# Average accuracy: 4673/5827: 0.802, 15 epochs with 70/30 split, 2-grams of POS tags

# Correctness: 4569/5827 -> 0.7841084606143813 8 epochs, 70/30 split
#-------------------------------

# Gutenberg
# British authors
# 723/2000 -> 0.3615

# -------------------------
# REUTERS Datset
# Accuracy:  43/45 = 0.956, 8 epochs, 3 authors, 50 articles / author, 70/30 train/test split

class AttentionRNNEncoder(nn.Module):
    # Parameters: input size (should match embedding layer), hidden size for the LSTM, dropout rate for the RNN,
    # and a boolean flag for whether or not we're using a bidirectional encoder
    def __init__(self, input_size, hidden_size, dropout, bidirect):
        super(AttentionRNNEncoder, self).__init__()
        self.bidirect = bidirect
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.reduce_h_W = nn.Linear(hidden_size * 2, hidden_size, bias=True)
        self.reduce_c_W = nn.Linear(hidden_size * 2, hidden_size, bias=True)
        self.rnn = nn.LSTM(input_size, hidden_size, num_layers=1, batch_first=True,
                           dropout=dropout, bidirectional=self.bidirect)
        self.init_weight()

    # Initializes weight matrices using Xavier initialization
    def init_weight(self):
        nn.init.xavier_uniform_(self.rnn.weight_hh_l0, gain=1)
        nn.init.xavier_uniform_(self.rnn.weight_ih_l0, gain=1)
        if self.bidirect:
            nn.init.xavier_uniform_(self.rnn.weight_hh_l0_reverse, gain=1)
            nn.init.xavier_uniform_(self.rnn.weight_ih_l0_reverse, gain=1)
        nn.init.constant_(self.rnn.bias_hh_l0, 0)
        nn.init.constant_(self.rnn.bias_ih_l0, 0)
        if self.bidirect:
            nn.init.constant_(self.rnn.bias_hh_l0_reverse, 0)
            nn.init.constant_(self.rnn.bias_ih_l0_reverse, 0)

    def get_output_size(self):
        return self.hidden_size * 2 if self.bidirect else self.hidden_size

    def sent_lens_to_mask(self, lens, max_length):
        return torch.from_numpy(np.asarray(
            [[1 if j < lens.data[i].item() else 0 for j in range(0, max_length)] for i in range(0, lens.shape[0])]))

    # embedded_words should be a [batch size x sent len x input dim] tensor
    # input_lens is a tensor containing the length of each input sentence
    # Returns output (each word's representation), context_mask (a mask of 0s and 1s
    # reflecting where the model's output should be considered), and h_t, a *tuple* containing
    # the final states h and c from the encoder for each sentence.
    def forward(self, embedded_words, input_lens):
        # Takes the embedded sentences, "packs" them into an efficient Pytorch-internal representation
        packed_embedding = nn.utils.rnn.pack_padded_sequence(embedded_words, input_lens, batch_first=True)
        # Runs the RNN over each sequence. Returns output at each position as well as the last vectors of the RNN
        # state for each sentence (first/last vectors for bidirectional)
        output, hn = self.rnn(packed_embedding)
        # Unpacks the Pytorch representation into normal tensors
        output, sent_lens = nn.utils.rnn.pad_packed_sequence(output)
        max_length = input_lens.data[0].item()
        context_mask = self.sent_lens_to_mask(sent_lens, max_length)

        # Grabs the encoded representations out of hn, which is a weird tuple thing.
        # Note: if you want multiple LSTM layers, you'll need to change this to consult the penultimate layer
        # or gather representations from all layers.
        if self.bidirect:
            h, c = hn[0], hn[1]
            # Grab the representations from forward and backward LSTMs
            h_, c_ = torch.cat((h[0], h[1]), dim=1), torch.cat((c[0], c[1]), dim=1)
            # Reduce them by multiplying by a weight matrix so that the hidden size sent to the decoder is the same
            # as the hidden size in the encoder
            new_h = self.reduce_h_W(h_)
            new_c = self.reduce_c_W(c_)
            h_t = (new_h, new_c)
        else:
            h, c = hn[0][0], hn[1][0]
            h_t = (h, c)
        return (output, context_mask, h_t)



class AttentionRNNDecoder(nn.Module):

    def __init__(self, hidden_size, embedding_dim, output_size, max_length, args):
        super(AttentionRNNDecoder, self).__init__()

        self.hidden_size = hidden_size
        self.embedding_dim = embedding_dim
        self.output_size = output_size

        # Bahdanau Attention Mechanism
        # self.attention = nn.Linear(self.hidden_size + self.embedding_dim, max_length)
        # self.attention_combine = nn.Linear((2 if args.bidirectional else 1) * self.hidden_size + self.embedding_dim,
        #                                    hidden_size)

        # self.attention = nn.Linear(self.embedding_dim, max_length)
        # self.attention_combine = nn.Linear((2 if args.bidirectional else 1) * self.hidden_size + self.embedding_dim,
        #                            hidden_size)

        real_hidden = (2 if args.bidirectional else 1) * self.hidden_size

        self.attention = nn.Linear(real_hidden, real_hidden)
        self.context = nn.Parameter(torch.FloatTensor(real_hidden, 1))
        nn.init.xavier_normal(self.context)

        # Neural Model
        # self.gru = nn.GRU(hidden_size, hidden_size, num_layers=1, batch_first=True)
        self.out = nn.Linear(real_hidden, output_size)
        self.softmax = nn.LogSoftmax(dim=1)

    def forward(self, enc_outputs):
        apply_attention = self.attention(enc_outputs)
        tanh_attention = F.tanh(apply_attention)

        multiplied = torch.matmul(tanh_attention, self.context)
        soft_multiplied = F.softmax(multiplied)

        apply_context = enc_outputs * soft_multiplied
        averaged = torch.sum(apply_context, dim=0)
        # compute_attention = torch.sum(enc_outputs * F.softmax(torch.matmul(F.tanh(self.attention(enc_outputs)), self.context)), dim=0)


        return self.softmax(self.out(averaged))








        # seq_len = enc_outputs.shape[0]
        # concat = torch.cat((start_token[0], hidden[0]), 1)
        # attention_weights = F.softmax(self.attention(concat)[:, :seq_len], dim=1)
        #
        # attention_applied = torch.bmm(attention_weights.unsqueeze(0), enc_outputs.squeeze(1).unsqueeze(0))
        #
        # output = torch.cat((start_token[0], attention_applied[0]), 1)
        # output = self.attention_combine(output).unsqueeze(0)
        # output = F.tanh(output)
        #
        # # output, hidden = self.gru(output, hidden)
        # output = self.softmax(self.out(output[0]))
        # return output, hidden


def _run_encoder(x_tensor, inp_lens_tensor, model_input_emb, model_enc):
    input_emb = model_input_emb.forward(x_tensor)
    enc_output_each_word, enc_context_mask, enc_final_states = model_enc.forward(input_emb, inp_lens_tensor)
    enc_final_states_reshaped = enc_final_states[0].unsqueeze(0).to(device), enc_final_states[1].unsqueeze(0).to(device)
    return enc_output_each_word, enc_context_mask, enc_final_states_reshaped


def _predict(decoder, enc_output_each_word, enc_hidden, output_indexer, model_output_emb):
    # decoder_input = torch.tensor([[output_indexer.index_of(SOS_SYMBOL)]]).to(device)
    # decoder_hidden = enc_hidden

    # run decoder, only once to get author classification
    # token_embedding = model_output_emb.forward(decoder_input)
    # decoder_output, decoder_hidden = decoder.forward(token_embedding, decoder_hidden, enc_output_each_word)

    decoder_output = decoder.forward(enc_output_each_word)
    decoder_output.to(device)

    predicted = torch.argmax(decoder_output)

    return predicted, decoder_output


def _run_decoder(decoder, enc_output_each_word, enc_hidden, output_tensor,
                 loss_function, output_indexer, model_output_emb):
    decoder_input = torch.tensor([[output_indexer.index_of(SOS_SYMBOL)]]).to(device)
    decoder_hidden = enc_hidden

    # run decoder, only once to get author classification
    # token_embedding = model_output_emb.forward(decoder_input).to(device)
    # decoder_output, decoder_hidden = decoder.forward(token_embedding, decoder_hidden, enc_output_each_word)
    # decoder_output = decoder_output.to(device)

    decoder_output = decoder.forward(enc_output_each_word)
    decoder_output.to(device)

    predicted = torch.argmax(decoder_output)

    # return predicted, decoder_output


    # loss w.r.t. true author
    loss = loss_function(decoder_output, output_tensor)

    predicted = torch.argmax(decoder_output)

    return predicted, loss


def _example(input_tensor, output_tensor, input_lens_tensor,
             encoder, decoder, model_input_emb, model_output_emb,
             optimizers, loss_function, input_indexer, output_indexer):
    # Run encoder
    enc_output_each_word, enc_context_mask, enc_hidden = _run_encoder(input_tensor, input_lens_tensor, model_input_emb, encoder)

    # zero grad
    for opt in optimizers:
        opt.zero_grad()

    # Run decoder, get loss
    prediction, loss = _run_decoder(decoder, enc_output_each_word, enc_hidden, output_tensor,
                                    loss_function, output_indexer, model_output_emb)

    loss.backward()

    for opt in optimizers:
        opt.step()

    return loss.item()


class DuAttentionModel(AuthorshipModel):
    def __init__(self, encoder, input_emb, decoder, output_emb, input_indexer, output_indexer, args, max_len):
        # Add any args you need here
        self.encoder = encoder
        self.decoder = decoder
        self.input_emb = input_emb
        self.output_emb = output_emb
        self.input_indexer = input_indexer
        self.output_indexer = output_indexer
        self.args = args
        self.max_len = max_len

    def _predictions(self, test_data, args):
        test_data.sort(key=lambda ex: len(word_tokenize(ex.passage)), reverse=True)

        input_lens = torch.LongTensor(np.asarray([len(word_tokenize(ex.passage)) for ex in test_data]))
        # input_max_len = torch.max(input_lens, dim=0)[0].item()
        all_test_input_data = torch.LongTensor(make_padded_input_tensor(test_data, self.input_indexer, self.max_len))
        all_test_output_data = torch.LongTensor(
            np.asarray([self.output_indexer.index_of(ex.author) for ex in test_data]))

        predictions = []
        probabilities = []
        for idx, X_batch in enumerate(all_test_input_data):
            X_batch = X_batch.unsqueeze(0).to(device)
            y_batch = all_test_output_data[idx].unsqueeze(0).to(device)
            input_lens_batch = input_lens[idx].unsqueeze(0).to(device)

            enc_output_each_word, enc_context_mask, enc_hidden = \
                _run_encoder(X_batch, input_lens_batch, self.input_emb, self.encoder)

            prediction, d_out = _predict(self.decoder, enc_output_each_word, enc_hidden, self.output_indexer, self.output_emb)

            predictions.append(self.output_indexer.get_object(prediction.item()))
            probabilities.append([np.exp(i.item()) for i in d_out[0]][:3])
        ids = [ex.id for ex in test_data]
        # probabilities = clf.predict_proba(vectorizer.transform(test_texts))
        if args.kaggle and args.train_type == "SPOOKY":
            with open("kaggle_out.csv", "w") as f:
                classes = ["id"] + [self.output_indexer.get_object(i) for i in range(len(self.output_indexer) - 1)]
                f.write(",".join(classes) + "\n")
                for id, probs in list(zip(ids, probabilities)):
                    probs = [str(i) for i in probs]
                    f.write(id + "," + ",".join(probs) + "\n")
        return predictions

    def myevaluate(self, test_data, args):
        test_data.sort(key=lambda ex: len(word_tokenize(ex.passage)), reverse=True)

        input_lens = torch.LongTensor(np.asarray([len(word_tokenize(ex.passage)) for ex in test_data]))
        # input_max_len = torch.max(input_lens, dim=0)[0].item()
        all_test_input_data = torch.LongTensor(make_padded_input_tensor(test_data, self.input_indexer, self.max_len))
        all_test_output_data = torch.LongTensor(
            np.asarray([self.output_indexer.index_of(ex.author) for ex in test_data]))

        correct = 0
        total = len(all_test_input_data)
        for idx, X_batch in enumerate(all_test_input_data):
            X_batch = X_batch.unsqueeze(0).to(device)
            y_batch = all_test_output_data[idx].unsqueeze(0).to(device)
            input_lens_batch = input_lens[idx].unsqueeze(0).to(device)

            enc_output_each_word, enc_context_mask, enc_hidden = \
                _run_encoder(X_batch, input_lens_batch, self.input_emb, self.encoder)

            prediction = _predict(self.decoder, enc_output_each_word, enc_hidden, self.output_indexer, self.output_emb)

            if prediction.item() == y_batch[0].item():
                correct += 1

        print("Correctness", str(correct) + "/" + str(total) + ": " + str(round(correct / total, 5)))


def train_du_attention_model_lstm(train_data, test_data, authors, word_vectors, args, pretrained=True):
    train_data.sort(key=lambda ex: len(word_tokenize(ex.passage)), reverse=True)
    word_indexer = word_vectors.word_indexer

    # Create indexed input
    print("creating indexed input")
    input_lens = torch.LongTensor(np.asarray([len(word_tokenize(ex.passage)) for ex in train_data])).to(device)
    test_input_lens = torch.LongTensor(np.asarray([len(word_tokenize(ex.passage)) for ex in test_data])).to(device)

    train_max_len = torch.max(input_lens, dim=0)[0].item()
    test_max_len = torch.max(test_input_lens, dim=0)[0].item()
    input_max_len = max(train_max_len, test_max_len)

    # input_max_len = np.max(np.asarray([len(word_tokenize(ex.passage)) for ex in train_data]))
    print("train input")
    all_train_input_data = torch.LongTensor(make_padded_input_tensor(train_data, word_indexer, input_max_len)).to(device)
    print("train output")
    all_train_output_data = torch.LongTensor(np.asarray([authors.index_of(ex.author) for ex in train_data])).to(device)

    # DataLoader constructs each batch from the given data
    input_size = args.embedding_size

    output_indexer = authors
    output_indexer.get_index(SOS_SYMBOL, True)
    output_size = len(authors) # TODO: this or + 1?

    if pretrained:
        input_emb = PretrainedEmbeddingLayer(word_vectors, args.emb_dropout).to(device)
    else:
        input_emb = RawEmbeddingLayer(args.embedding_size, len(word_indexer), args.emb_dropout).to(device)

    encoder = AttentionRNNEncoder(input_size, args.hidden_size, args.rnn_dropout, args.bidirectional).to(device)
    output_emb = RawEmbeddingLayer(100, len(output_indexer), 0.2).to(device)
    decoder = AttentionRNNDecoder(args.hidden_size, 100, output_size, input_max_len, args).to(device)

    # Construct optimizer. Using Adam optimizer
    params = list(encoder.parameters()) + list(input_emb.parameters()) \
             + list(decoder.parameters()) + list(output_emb.parameters())
    lr = args.lr
    optimizer = Adam(params, lr=lr)

    loss_function = nn.NLLLoss()
    num_epochs = args.epochs

    for epoch in range(num_epochs):

        epoch_loss = 0

        # for X_batch, y_batch, input_lens_batch in train_batch_loader:
        for idx, X_batch in enumerate(all_train_input_data):
            if idx % 100 == 0:
                print("Example", idx, "out of", len(all_train_input_data))
            X_batch = X_batch.unsqueeze(0).to(device)
            y_batch = all_train_output_data[idx].unsqueeze(0).to(device)
            input_lens_batch = input_lens[idx].unsqueeze(0).to(device)

            epoch_loss += _example(X_batch, y_batch, input_lens_batch, encoder, decoder, input_emb, output_emb,
                                   [optimizer],
                                   loss_function, word_indexer, output_indexer)

        print("Epoch " + str(epoch) + " Loss:", epoch_loss)
        # if epoch == 0:
        #EncDecTrainedModel(encoder, input_emb, decoder, output_emb, word_indexer, authors, args, input_max_len).evaluate(test_data)

    return DuAttentionModel(encoder, input_emb, decoder, output_emb, word_indexer, authors, args, input_max_len)
