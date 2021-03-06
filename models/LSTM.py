import torch
import torch.nn as nn
import torch.nn.functional as F
import random
from torch.autograd import Variable as Var
from torch.optim import Adam
from torch.utils.data import DataLoader, TensorDataset

from models.attention import RawEmbeddingLayer
from utils import *
import numpy as np

# Run on gpu is present
device = 'cuda' if torch.cuda.is_available() else 'cpu'

# Embedding layer that has a lookup table of symbols that is [full_dict_size x input_dim]. Includes dropout.
# Works for both non-batched and batched inputs
class EmbeddingLayer(nn.Module):
    # Parameters: dimension of the word embeddings, number of words, and the dropout rate to apply
    # (0.2 is often a reasonable value)
    def __init__(self, word_vectors, embedding_dropout_rate):
        super(EmbeddingLayer, self).__init__()
        self.dropout = nn.Dropout(embedding_dropout_rate)
        self.word_embedding = nn.Embedding.from_pretrained(torch.from_numpy(word_vectors.vectors).float(), False)
        self.word_vectors = word_vectors

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


# One-layer RNN encoder for batched inputs -- handles multiple sentences at once. You're free to call it with a
# leading dimension of 1 (batch size 1) but it does expect this dimension.
class RNNEncoder(nn.Module):
    # Parameters: input size (should match embedding layer), hidden size for the LSTM, dropout rate for the RNN,
    # and a boolean flag for whether or not we're using a bidirectional encoder
    def __init__(self, input_size, hidden_size, output_size, dropout, bidirect=True):
        super(RNNEncoder, self).__init__()
        self.bidirect = bidirect
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.reduce_h_W = nn.Linear(hidden_size * 2, hidden_size, bias=True)
        self.reduce_c_W = nn.Linear(hidden_size * 2, hidden_size, bias=True)
        self.rnn = nn.LSTM(input_size, hidden_size, num_layers=1, batch_first=True,
                           dropout=dropout, bidirectional=self.bidirect)

        self.hiddenToLabel = nn.Linear(hidden_size, self.output_size)
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
        nn.init.xavier_uniform_(self.hiddenToLabel.weight)

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
        labels = self.hiddenToLabel(h_t[0])
        probs = F.log_softmax(labels[0], dim=0)
        return (probs, h_t)


# American authors
# Average accuracy: .416 with 10 passages/book/author, 4 authors (old test)
# Average accuracy: .583 with 30 passages/book/author, 4 authors (old test)
# Average accuracy: .675 with 50 passages/book/author, 4 authors (old test)

# British authors
# Average accuracy: .333 with 30 passages/book/author, 5 authors (old test)
# Average accuracy: .425 with 30 passages/book/author, 5 authors (old test)

# Combined authors
# Average accuracy: .264 with 10 passages/book/author, 9 authors (old test)
# Average accuracy: .2745 with 30 passages/book/author, 9 authors (old test)

# ----------------------------------------------------------

# British Authors
# Average accuracy: .196 with 30 passages/book/author, 5 authors (new test)

# Average accuracy: 687/2400 = .286 with 200 sentences/book/author, 5 authors (new test)
# Average accuracy: 1432/4000 = .358 with 400 sentences/book/author, 5 authors (new test)

# Average accuracy: 642/2000 = .286 with 200 sentences/book/author, 5 authors (new test) WITH POS EMBEDDINGS (1 gram)
# Average accuracy: 1327/4000 = .332 with 400 sentences/book/author, 5 authors (new test) WITH POS EMBEDDINGS (1 gram)
#-------------------------------------------------------------
# SPOOKY Dataset (70-30 split)
# One run, Glove word embeddings. 4411 / 5827 = 0.75699 with 10 epochs
# REUTERS:
# Correctness: 140/150 -> 0.9333333333333333

class LSTMTrainedModel(AuthorshipModel):
    def __init__(self, model, model_emb, indexer, authors, history=None):
        # Add any args you need here
        self.model = model
        self.model_emb = model_emb
        self.word_indexer = indexer
        self.authors = authors
        self.history = history
    def _predictions(self, test_data, args):

        predictions = []
        test_data.sort(key=lambda ex: len(word_tokenize(ex.passage)), reverse=True)

        with torch.no_grad():
            self.model.eval()
            self.model_emb.eval()
            input_lens = torch.LongTensor(np.asarray([len(word_tokenize(ex.passage)) for ex in test_data]))
            input_max_len = torch.max(input_lens, dim=0)[0].item()
            all_test_input_data = torch.LongTensor(make_padded_input_tensor(test_data, self.word_indexer, input_max_len))
            all_test_output_data = torch.LongTensor(np.asarray([self.authors.index_of(ex.author) for ex in test_data]))

            for idx, X_batch in enumerate(all_test_input_data):
                print(X_batch)
                y_batch = all_test_output_data[idx].unsqueeze(0)
                print(y_batch)
                input_lens_batch = input_lens[idx].unsqueeze(0).to(device)

                # Get word embeddings
                embedded_words = self.model_emb.forward(X_batch.unsqueeze(0).to(device)).to(device)

                # Get probability and hidden state
                probs, hidden = self.model.forward(embedded_words, input_lens_batch)

                predictions.append(self.authors.get_object(torch.argmax(probs).item()))

        return predictions

    def myevaluate(self, test_data, args):
        test_data.sort(key=lambda ex: len(word_tokenize(ex.passage)), reverse=True)

        with torch.no_grad():
            self.model.eval()
            self.model_emb.eval()
            input_lens = torch.LongTensor(np.asarray([len(word_tokenize(ex.passage)) for ex in test_data]))
            input_max_len = torch.max(input_lens, dim=0)[0].item()
            all_test_input_data = torch.LongTensor(make_padded_input_tensor(test_data, self.word_indexer, input_max_len))
            all_test_output_data = torch.LongTensor(np.asarray([self.authors.index_of(ex.author) for ex in test_data]))

            correct = 0        
            total = len(all_test_input_data)
            for idx, X_batch in enumerate(all_test_input_data):
                print(X_batch)
                y_batch = all_test_output_data[idx].unsqueeze(0)
                print(y_batch)
                input_lens_batch = input_lens[idx].unsqueeze(0).to(device)

                # Get word embeddings
                embedded_words = self.model_emb.forward(X_batch.unsqueeze(0).to(device)).to(device)
                
                # Get probability and hidden state
                probs, hidden = self.model.forward(embedded_words, input_lens_batch)
                print(probs, max(probs))
                if torch.argmax(probs).item() == y_batch[0].item():
                    correct += 1

        print("Correctness", str(correct) + "/" + str(total) + ": " + str(round(correct/total, 5)))

        return correct, total


def train_lstm_model(train_data, test_data, authors, word_vectors, args, pretrained=True):
    train_data.sort(key=lambda ex: len(word_tokenize(ex.passage)), reverse=True)

    word_indexer = word_vectors.word_indexer

    # Create indexed input
    print("creating indexed input")
    input_lens = torch.LongTensor(np.asarray([len(word_tokenize(ex.passage)) for ex in train_data]))
    input_max_len = torch.max(input_lens, dim=0)[0].item()
    # input_max_len = np.max(np.asarray([len(word_tokenize(ex.passage)) for ex in train_data]))
    print("train input")
    all_train_input_data = torch.LongTensor(make_padded_input_tensor(train_data, word_indexer, input_max_len))
    print("train output")
    all_train_output_data = torch.LongTensor(np.asarray([authors.index_of(ex.author) for ex in train_data]))

    input_size = args.embedding_size
    output_size = len(authors)

    if pretrained:
        model_emb = EmbeddingLayer(word_vectors, args.emb_dropout).to(device)
    else:
        model_emb = RawEmbeddingLayer(args.embedding_size, len(word_indexer), args.emb_dropout).to(device)
    encoder = RNNEncoder(input_size, args.hidden_size, output_size, args.rnn_dropout).to(device)

    # Construct optimizer. Using Adam optimizer
    params = list(encoder.parameters()) + list(model_emb.parameters())
    lr = args.lr
    optimizer = Adam(params, lr=lr)

    loss_function = nn.NLLLoss()
    num_epochs = args.epochs

    encoder.train()
    model_emb.train()

    loss_history = []

    for epoch in range(num_epochs):

        epoch_loss = 0

        #for X_batch, y_batch, input_lens_batch in train_batch_loader:
        for idx, X_batch in enumerate(all_train_input_data):
            if idx % 100 == 0:
                print("Example", idx, "out of", len(all_train_input_data))
            y_batch = all_train_output_data[idx].unsqueeze(0).to(device)
            input_lens_batch = input_lens[idx].unsqueeze(0).to(device)

            # Initialize optimizer
            optimizer.zero_grad()

            # Get word embeddings
            embedded_words = model_emb.forward(X_batch.unsqueeze(0).to(device)).to(device)

            # Get probability and hidden state
            probs, hidden = encoder.forward(embedded_words, input_lens_batch)
            #print(probs)
            #print("Predicted", torch.argmax(probs,0), "|| Actual" ,y_batch)
            loss = loss_function(probs.unsqueeze(0).to(device), y_batch)
            epoch_loss += loss

            # Run backward
            loss.backward()
            optimizer.step()
        print("Epoch " + str(epoch) + " Loss:", epoch_loss)
        loss_history.append(epoch_loss.item())

    
    return LSTMTrainedModel(encoder, model_emb, word_indexer, authors, loss_history)
