#coding:utf-8
import torch
import numpy as np
from torch.autograd import Variable
import torch.nn as nn
import torch.nn.functional as F
from torch.nn import utils as nn_utils
from torch.nn.parameter import Parameter
import dgl
from dgl.nn import GATConv

VERY_SMALL_NUMBER = 1e-10
VERY_NEG_NUMBER = -100000000000


def use_cuda(var):
    if torch.cuda.is_available():
        return var.cuda()
    else:
        return var


class GAT(nn.Module):
	def __init__(self, features):
		super(GAT, self).__init__()
		self.gat1 = GATConv(in_feats=features, out_feats=features, num_heads=1)
		self.gat2 = GATConv(in_feats=features, out_feats=features, num_heads=1)

	def forward(self, g, inputs):
		if g.number_of_edges() == 0:
			return inputs
		h = self.gat1(g, inputs)
		h = torch.relu(h)
		h = self.gat2(g, h)
		return h


class ConceptFlow(nn.Module):
    def __init__(self, config, word_embed, entity_embed, adj_table):
        super(ConceptFlow, self).__init__()
        self.is_inference = False
        # Encoder
        self.fact_scale = config.fact_scale
        self.trans_units = config.trans_units
        self.embed_units = config.embed_units
        self.units = config.units
        self.layers = config.layers
        self.gnn_layers = config.gnn_layers
        self.symbols = config.symbols
        self.bs_width = config.beam_search_width
        self.max_hop = config.max_hop

        self.adj_table = adj_table
        self.word_embedding = nn.Embedding(num_embeddings=word_embed.shape[0], embedding_dim=self.embed_units, padding_idx=0)
        self.word_embedding.weight = nn.Parameter(use_cuda(torch.Tensor(word_embed)))
        self.word_embedding.weight.requires_grad = True

        self.entity_embedding = nn.Embedding(num_embeddings=entity_embed.shape[0] + 7, embedding_dim=self.trans_units, padding_idx=0)
        entity_embed = torch.Tensor(entity_embed)

        entity_embed = torch.cat((torch.zeros(7, self.trans_units), entity_embed), 0)
        self.entity_embedding.weight = nn.Parameter(use_cuda(torch.Tensor(entity_embed)))
        self.entity_embedding.weight.requires_grad = True
        self.entity_linear = nn.Linear(in_features=self.trans_units, out_features=self.trans_units)

        self.softmax_d0 = nn.Softmax(dim=0)
        self.softmax_d1 = nn.Softmax(dim=1)
        self.softmax_d2 = nn.Softmax(dim=2)
        self.relu = nn.ReLU()

        self.text_encoder = nn.GRU(input_size=self.embed_units, hidden_size=self.units, num_layers=self.layers, batch_first=True)
        self.graph_decoder = nn.LSTM(input_size=self.units, hidden_size=self.units, num_layers=self.layers, batch_first=True)
        self.decoder = nn.GRU(input_size=self.units + self.embed_units, hidden_size=self.units, num_layers=self.layers, batch_first=True)

        self.attn_c_linear = nn.Linear(in_features=self.units, out_features=self.units, bias=False)
        self.context_linear = nn.Linear(in_features=2 * self.units + self.trans_units, out_features=self.units, bias=False)

        # matrix for graph attention
        self.graph_attn_linear = nn.Linear(in_features=self.trans_units, out_features=self.units, bias=False)
        # linear layer to convert z in graph decoder
        self.graph_convt_linear = nn.Linear(in_features=self.trans_units + self.units, out_features=self.units)
        self.graph_prob_linear = nn.Linear(in_features=self.units, out_features=self.trans_units, bias=False)
        self.bias = Parameter(torch.FloatTensor(1).zero_())

        # GAT
        self.GAT = GAT(self.trans_units)

        # Loss
        self.logits_linear = nn.Linear(in_features=self.units, out_features=self.symbols)
        self.selector_linear = nn.Linear(in_features=self.units, out_features=2)

    def forward(self, batch_data):
        query_text = batch_data['post_text']    # todo: rename query_text to post_text
        response_text = batch_data['response_text']
        responses_length = batch_data['responses_length']
        post_ent = batch_data['post_ent']
        post_ent_len = batch_data['post_ent_len']
        response_ent = batch_data['response_ent']
        subgraph = batch_data['subgraph']
        subgraph_len = batch_data['subgraph_size']
        match_entity = batch_data['match_entity']
        edges = batch_data['edges']

        if not self.is_inference:
            graph_input = batch_data['graph_input']
            output_mask = batch_data['output_mask']
            max_path_num = graph_input.shape[1]
            max_path_len = graph_input.shape[2]
            path_num = batch_data['path_num']
            path_len = batch_data['path_len']

        if self.is_inference == True:
            word2id = batch_data['word2id']
            entity2id = batch_data['entity2id']
            id2entity = dict()
            for key in entity2id.keys():
                id2entity[entity2id[key]] = key
        else:
            id2entity = None

        batch_size = query_text.shape[0]
        # numpy to tensor
        query_text = use_cuda(Variable(torch.from_numpy(query_text).type('torch.LongTensor'), requires_grad=False))
        response_text = use_cuda(Variable(torch.from_numpy(response_text).type('torch.LongTensor'), requires_grad=False))
        responses_length = use_cuda(Variable(torch.Tensor(responses_length).type('torch.LongTensor'), requires_grad=False))
        query_mask = use_cuda((query_text != 0).type('torch.FloatTensor'))

        if not self.is_inference:
            graph_input = use_cuda(Variable(torch.from_numpy(graph_input).type('torch.LongTensor'), requires_grad=False))
            output_mask = use_cuda(Variable(torch.from_numpy(output_mask).type('torch.LongTensor'), requires_grad=False))
            graph_target = use_cuda(torch.FloatTensor(output_mask.size()).fill_(0))
            total_sample = torch.sum(output_mask)
            for i in range(batch_size):
                for j in range(path_num[i]):
                    for k in range(path_len[i][j]):
                        graph_target[i, j, k, 0].fill_(1)

        decoder_len = response_text.shape[1]
        responses_target = response_text
        responses_id = torch.cat((use_cuda(torch.ones([batch_size, 1]).type('torch.LongTensor')),torch.split(response_text, [decoder_len - 1, 1], 1)[0]), 1)

        # text encoder
        text_encoder_input = self.word_embedding(query_text)
        text_encoder_output, text_encoder_state = self.text_encoder(text_encoder_input, use_cuda(Variable(torch.zeros(self.layers, batch_size, self.units))))

        # graph decoder
        if not self.is_inference:
            graph_input = graph_input.contiguous().view(batch_size * max_path_num, max_path_len, -1)    # todo: use reshape?
            # get graph_context of shape (bs * max_path_num, 1, D), D is the dimension of hidden states
            text_hidden_state = text_encoder_state[self.layers - 1].unsqueeze(1)   # use the hidden states of the last layer in t=seq_len
            graph_context = use_cuda(torch.empty(0))
            for b in range(batch_size):
                for n in range(max_path_num):
                    graph_context = torch.cat([graph_context, text_hidden_state[b:b+1, :, :]], dim=0)
            graph_decoder_state = self.init_hidden(self.layers, batch_size * max_path_num, self.units)
            for t in range(max_path_len):
                if t == 0:
                    h = graph_context
                else:
                    ground_truth_ent = graph_input[:, t-1:t, 0]
                    embed = self.entity_embedding(ground_truth_ent)     # (bs * max_num, 1, trans_units)
                    graph_context = self.graph_convt_linear(torch.cat([graph_context, embed], dim=2))
                    graph_output, graph_decoder_state = self.graph_decoder(graph_context, graph_decoder_state)
                    h = torch.cat([h, graph_output], dim=1)
            # size of h: (batch*max_num, max_len, D)
            input_embed = self.entity_embedding(graph_input)
            logits = torch.matmul(input_embed, self.graph_prob_linear(h).unsqueeze(3)).reshape(batch_size, max_path_num, max_path_len, -1)
            logits += self.bias
            retrieval_loss = F.binary_cross_entropy_with_logits(logits, graph_target, weight=output_mask, reduction='sum')
            retrieval_loss /= total_sample

        else:
            subgraph = []
            edges = []
            subgraph_len = []
            match_entity = [[] for bs in range(batch_size)]
            for b in range(batch_size):
                for t in range(self.max_hop + 1):
                    if t == 0:  # select zero-hop
                        graph_context = text_encoder_state[self.layers-2: self.layers-1, b: b+1, :].transpose(0, 1)
                        graph_decoder_state = self.init_hidden(self.layers, 1, self.units)
                        graph_output, graph_decoder_state = self.graph_decoder(graph_context, graph_decoder_state)
                        candidates = use_cuda(torch.LongTensor(post_ent[b]))
                        candidate_embed = self.entity_embedding(candidates)  # (N, trans_units), N is the size of candidates
                        logits = torch.matmul(candidate_embed, self.graph_prob_linear(graph_output.squeeze()))      # (N)
                        logits += self.bias
                        prob = torch.sigmoid(logits).detach().cpu().numpy().tolist()
                        sorted_prob = [[i, prob[i]] for i in range(len(prob))]
                        sorted_prob.sort(key=lambda x: x[1], reverse=True)
                        sorted_prob = sorted_prob[:self.bs_width]
                        next_ent = [post_ent[b][x[0]] for x in sorted_prob]
                        next_ent += [1] * (self.bs_width - len(next_ent))   # 1 for padding
                        all_paths = [[x] for x in next_ent]
                        paths_prob = [x[1] for x in sorted_prob]

                        # get graph context for all paths
                        # use the 'batch' dimension for multiple paths
                        all_graph_context, all_state1, all_state2 = use_cuda(torch.empty(0)), use_cuda(torch.empty(0)), use_cuda(torch.empty(0))
                        for i in range(len(next_ent)):
                            all_state1 = torch.cat([all_state1, graph_decoder_state[0]], dim=1)
                            all_state2 = torch.cat([all_state2, graph_decoder_state[1]], dim=1)
                            all_graph_context = torch.cat([all_graph_context, graph_context], dim=0)
                        all_graph_decoder_state = (all_state1, all_state2)
                        continue
                    # assert len(all_paths) == len(next_ent)
                    embed = self.entity_embedding(use_cuda(torch.LongTensor(next_ent))).unsqueeze(1)
                    all_graph_context = self.graph_convt_linear(torch.cat([all_graph_context, embed], dim=2))
                    all_graph_output, all_graph_decoder_state = self.graph_decoder(all_graph_context, all_graph_decoder_state)
                    all_logits = use_cuda(torch.empty(0))

                    past_probs = []
                    path_candidates = []
                    for i, e in enumerate(next_ent):
                        if e != 0 and e != 1:
                            candidates = [x for x in self.adj_table[e] if x not in all_paths[i]] + [0]
                            path_candidates += [all_paths[i] + [x] for x in candidates]
                            past_probs += [paths_prob[i]] * len(candidates)
                            candidate_embed = self.entity_embedding(use_cuda(torch.LongTensor(candidates)))
                            graph_output = all_graph_output.squeeze()[i]
                            logits = torch.matmul(candidate_embed, self.graph_prob_linear(graph_output))
                            logits += self.bias
                            all_logits = torch.cat([all_logits, logits], dim=0)
                    all_probs = torch.sigmoid(all_logits).detach().cpu().numpy().tolist()
                    all_probs = [all_probs[i] * past_probs[i] for i in range(len(all_probs))]
                    sorted_prob = [[i, all_probs[i]] for i in range(len(all_probs))]
                    sorted_prob.sort(key=lambda x: x[1], reverse=True)
                    sorted_prob = sorted_prob[: self.bs_width]
                    index = 0
                    new_ent = []
                    new_paths = []
                    new_prob = []
                    for i in range(self.bs_width):
                        if next_ent[i] == 0:
                            new_ent.append(0)
                            new_paths.append(all_paths[i])
                            new_prob.append(paths_prob[i])
                        else:
                            new_ent.append(path_candidates[sorted_prob[index][0]][-1])
                            new_paths.append(path_candidates[sorted_prob[index][0]])
                            new_prob.append(sorted_prob[index][1])
                            index += 1
                            if index >= len(sorted_prob):
                                break
                    next_ent = new_ent + [1] * (self.bs_width - len(new_ent))
                    all_paths = new_paths
                    paths_prob = new_prob
                    if sum(next_ent) == 0:
                        break
                pass

                graph_nodes = set()
                graph_edges = [[], []]
                for path in all_paths:
                    prior = None
                    for node in path:
                        if node == 0:
                            break
                        graph_nodes.add(node)
                        if prior:
                            head = max(prior, node)
                            tail = prior + node - head
                            graph_edges[0] += [head, tail]
                            graph_edges[1] += [tail, head]
                        prior = node
                graph_nodes = list(graph_nodes)
                subgraph.append(graph_nodes)
                subgraph_len.append(len(graph_nodes))
                edges.append(graph_edges)
                # process match_entity
                g2l = dict()
                for i in range(len(graph_nodes)):
                    g2l[graph_nodes[i]] = i
                for i in range(len(response_ent[b])):
                    index = g2l[response_ent[b][i]] if response_ent[b][i] in g2l else -1
                    match_entity[b].append(index)
        max_graph_size = max(subgraph_len)
        if self.is_inference:
            for b in range(batch_size):
                match_entity[b] += [-1] * (max_graph_size - len(match_entity[b]))

        # get subgraph representation
        graph_list = self.construct_graph(subgraph, edges)
        batched_graph = dgl.batch(graph_list)
        graph_embed = self.gnn(batched_graph, batched_graph.ndata['h'])
        # text decoder input
        decoder_input = self.word_embedding(responses_id)

        # attention
        c_attention_keys = self.attn_c_linear(text_encoder_output)
        c_attention_values = text_encoder_output

        decoder_state = text_encoder_state
        decoder_output = use_cuda(torch.empty(0))
        ce_alignments = use_cuda(torch.empty(0))

        context = use_cuda(torch.zeros([batch_size, self.units]))
        # train
        graph_mask = np.zeros([batch_size, graph_embed.shape[1]])
        for i in range(batch_size):
            graph_mask[i][0: subgraph_len[i]] = 1
        graph_mask = use_cuda(torch.from_numpy(graph_mask).type('torch.LongTensor'))

        ce_attention_keys = self.graph_attn_linear(graph_embed)
        ce_attention_values = graph_embed
        for t in range(decoder_len):
            decoder_input_t = torch.cat((decoder_input[:,t,:], context), 1).unsqueeze(1)
            decoder_output_t, decoder_state = self.decoder(decoder_input_t, decoder_state)

            context, ce_alignments_t = self.attention(c_attention_keys, c_attention_values, ce_attention_keys, ce_attention_values,
                decoder_output_t.squeeze(1), graph_mask)
            decoder_output_t = context.unsqueeze(1)
            decoder_output = torch.cat((decoder_output, decoder_output_t), 1)
            ce_alignments = torch.cat((ce_alignments, ce_alignments_t.unsqueeze(1)), 1)

        if self.is_inference:   # test
            word_index = use_cuda(torch.empty(0).type('torch.LongTensor'))
            decoder_input_t = self.word_embedding(use_cuda(torch.ones([batch_size]).type('torch.LongTensor')))
            context = use_cuda(torch.zeros([batch_size, self.units]))
            decoder_state = text_encoder_state
            selector = use_cuda(torch.empty(0).type('torch.LongTensor'))

            for t in range(decoder_len):
                decoder_input_t = torch.cat((decoder_input_t, context), 1).unsqueeze(1)
                decoder_output_t, decoder_state = self.decoder(decoder_input_t, decoder_state)

                context, ce_alignments_t = self.attention(c_attention_keys, c_attention_values, ce_attention_keys, ce_attention_values,
                                                              decoder_output_t.squeeze(1), graph_mask)
                decoder_output_t = context.unsqueeze(1)
                decoder_input_t, word_index_t, selector_t = self.inference(decoder_output_t, ce_alignments_t, word2id, subgraph, id2entity)
                word_index = torch.cat((word_index, word_index_t.unsqueeze(1)), 1)
                selector = torch.cat((selector, selector_t.unsqueeze(1)), 1)

        ### Total Loss
        decoder_mask = np.zeros([batch_size, decoder_len])
        for i in range(batch_size):
            decoder_mask[i][0:responses_length[i]] = 1
        decoder_mask = use_cuda(torch.from_numpy(decoder_mask).type('torch.LongTensor'))

        graph_entities = use_cuda(torch.zeros(batch_size, decoder_len, max_graph_size))
        # if not self.is_inference:
        for b in range(batch_size):
            for d in range(decoder_len):
                if match_entity[b][d] != -1:
                    graph_entities[b][d][match_entity[b][d]] = 1

        # get recall & precision
        if self.is_inference:
            response_ent_num = 0
            found_num = 0
            for b in range(batch_size):
                entities = set()
                for i in range(len(subgraph[b])):
                    if subgraph[b][i] > 0:
                        entities.add(subgraph[b][i])
                for d in range(decoder_len):
                    if response_ent[b][d] == -1:
                        continue
                    response_ent_num += 1
                    if response_ent[b][d] in entities:
                        found_num += 1
            total_graph_size = sum(subgraph_len)
            recall = found_num / response_ent_num
            precision = found_num / total_graph_size

        use_entities_local = torch.sum(graph_entities, [2])

        decoder_loss, sentence_ppx, sentence_ppx_word, sentence_ppx_entity, word_neg_num, local_neg_num = \
            self.total_loss(decoder_output, responses_target, decoder_mask, ce_alignments, use_entities_local, graph_entities)

        if self.is_inference:
            return decoder_loss, sentence_ppx, sentence_ppx_word, sentence_ppx_entity, word_neg_num, local_neg_num, \
                   recall, precision, total_graph_size, word_index.detach().cpu().numpy().tolist()
        return decoder_loss, retrieval_loss, sentence_ppx, sentence_ppx_word, sentence_ppx_entity, word_neg_num, local_neg_num

    def inference(self, decoder_output_t, ce_alignments_t, word2id, local_entity, id2entity):
        '''
        decoder_output_t: [batch_size, 1, self.units]
        ce_alignments_t: [batch_size, local_entity_len]
        '''
        batch_size = decoder_output_t.shape[0]

        logits = self.logits_linear(decoder_output_t.squeeze(1)) # (bs, num_symbols)

        selector = self.softmax_d1(self.selector_linear(decoder_output_t.squeeze(1)))   # (bs, 2)

        # get the probablities and indices of choosen tokens
        (word_prob, word_t) = torch.max(selector[:, 0].unsqueeze(1) * self.softmax_d1(logits), dim=1)
        (entity_prob, entity_index_t) = torch.max(selector[:, 1].unsqueeze(1) * ce_alignments_t, dim=1)

        selector[:,0] = selector[:,0] * word_prob
        selector[:,1] = selector[:,1] * entity_prob
        # selector[:, 0] = word_prob
        # selector[:, 1] = entity_prob
        selector = torch.argmax(selector, dim=1)

        entity_index_t = entity_index_t.cpu().numpy().tolist()
        word_t = word_t.cpu().numpy().tolist()

        word_local_entity_t = []
        word_only_two_entity_t = []
        word_index_final_t = []
        for i in range(batch_size):
            if selector[i] == 0:
                word_index_final_t.append(word_t[i])
            elif selector[i] == 1:
                local_entity_index_t = int(local_entity[i][entity_index_t[i]])
                local_entity_text = id2entity[local_entity_index_t]
                if local_entity_text not in word2id:
                    local_entity_text = '_UNK'
                word_index_final_t.append(word2id[local_entity_text])

        word_index_final_t = use_cuda(torch.LongTensor(word_index_final_t))
        decoder_input_t = self.word_embedding(word_index_final_t)

        return decoder_input_t, word_index_final_t, selector

    def total_loss(self, decoder_output, responses_target, decoder_mask, ce_alignments, use_entities_local, entity_targets_local):
        batch_size = decoder_output.shape[0]
        decoder_len = responses_target.shape[1]

        local_masks = use_cuda(decoder_mask.reshape([-1]).type("torch.FloatTensor"))
        local_masks_word = use_cuda((1 - use_entities_local).reshape([-1]).type("torch.FloatTensor")) * local_masks
        local_masks_local = use_cuda(use_entities_local.reshape([-1]).type("torch.FloatTensor"))
        logits = self.logits_linear(decoder_output) # (bs, decoder_len, num_symbols)

        word_prob = torch.gather(self.softmax_d2(logits), 2, responses_target.unsqueeze(2)).squeeze(2)  # (bs, decoder_len)

        selector_word, selector_local = torch.split(self.softmax_d2(self.selector_linear(decoder_output)), [1, 1], 2) # (bs, decoder_len, 1)
        selector_word = selector_word.squeeze(2)
        selector_local = selector_local.squeeze(2)
        entity_prob_local = torch.sum(ce_alignments * entity_targets_local, [2])

        ppx_prob = word_prob * (1 - use_entities_local) + entity_prob_local * use_entities_local
        ppx_word = word_prob * (1 - use_entities_local)
        ppx_local = entity_prob_local * use_entities_local

        final_prob = word_prob * selector_word * (1 - use_entities_local) + entity_prob_local * selector_local * use_entities_local

        final_loss = torch.sum(-torch.log(1e-12 + final_prob).reshape([-1]) * local_masks)

        sentence_ppx = torch.sum((-torch.log(1e-12 + ppx_prob).reshape([-1]) * local_masks).reshape([batch_size, -1]), 1)
        sentence_ppx_word = torch.sum((-torch.log(1e-12 + ppx_word).reshape([-1]) * local_masks_word).reshape([batch_size, -1]), 1)
        sentence_ppx_local = torch.sum((-torch.log(1e-12 + ppx_local).reshape([-1]) * local_masks_local).reshape([batch_size, -1]), 1)

        selector_loss = torch.sum(-torch.log(1e-12 + selector_local * use_entities_local + selector_word * (1 - use_entities_local)).reshape([-1]) * local_masks)

        loss = final_loss + selector_loss
        total_size = torch.sum(local_masks)
        total_size += 1e-12

        sum_word = torch.sum(use_cuda(((1 - use_entities_local) * use_cuda(decoder_mask.type("torch.FloatTensor"))).type("torch.FloatTensor")), 1)
        sum_local = torch.sum(use_cuda(use_entities_local.type("torch.FloatTensor")), 1)

        word_neg_mask = use_cuda((sum_word == 0).type("torch.FloatTensor"))
        local_neg_mask = use_cuda((sum_local == 0).type("torch.FloatTensor"))

        word_neg_num = torch.sum(word_neg_mask)
        local_neg_num = torch.sum(local_neg_mask)

        sum_word = sum_word + word_neg_mask
        sum_local = sum_local + local_neg_mask

        return loss/total_size, sentence_ppx/torch.sum(use_cuda(decoder_mask.type("torch.FloatTensor")), 1), \
               sentence_ppx_word/sum_word, sentence_ppx_local/sum_local, word_neg_num, local_neg_num

    def attention(self, c_attention_keys, c_attention_values, ce_attention_keys, ce_attention_values, decoder_state, graph_mask):
        batch_size = c_attention_keys.shape[0]

        c_query = decoder_state.reshape([-1, 1, self.units])
        ce_query = decoder_state.reshape([-1, 1, self.units])

        c_scores = torch.sum(c_attention_keys * c_query, 2)
        ce_scores = torch.sum(ce_attention_keys * ce_query, 2)

        c_alignments = self.softmax_d1(c_scores)
        ce_alignments = self.softmax_d1(ce_scores)
        ce_alignments = ce_alignments * use_cuda(graph_mask.type("torch.FloatTensor"))

        c_context = torch.sum(c_alignments.unsqueeze(2) * c_attention_values, 1)
        ce_context = torch.sum(ce_alignments.unsqueeze(2) * ce_attention_values, 1)

        context = self.context_linear(torch.cat((decoder_state, c_context, ce_context), 1))
        return context, ce_alignments

    def beam_search(self, decoder_state, current_graph, outer):
        batch_size = decoder_state.shape[0]
        paths = []
        for i, d in enumerate(outer):
            path = dict()
            graph = set(current_graph[i])
            for o in d:
                out = o[-1]
                for node in self.adj_table[out]:
                    if node in graph:
                        continue
                    if node in path:
                        path[node].append(o + [node])
                    else:
                        path[node] = [o + [node]]
            paths.append(path)
        # choose the path with largest attention score for every nodes in outmost hop
        paths_embed = []
        nodes_list = []
        for d in paths:
            node_list = []
            path_embed = []
            for node in d:
                path = d[node]
                for i, p in enumerate(path):
                    node_list.append([node, p])
                    p_embed = torch.sum(self.entity_embedding(use_cuda(torch.Tensor(p).long())), 0).detach()
                    p_embed = p_embed.cpu().numpy().tolist()
                    path_embed.append(p_embed)
            paths_embed.append(path_embed)
            nodes_list.append(node_list)
        max_path_num = max([len(pp) for pp in paths_embed])
        padded_paths_embed = \
            [pp + [[0 for j in range(100)] for i in range((max_path_num - len(pp)))] for pp in paths_embed]
        paths_embed = use_cuda(torch.Tensor(padded_paths_embed))
        c_query = decoder_state.unsqueeze(1)
        scores = torch.sum(c_query * self.graph_attn_linear(paths_embed), dim=2)    # (bs, max_path_num)
        scores = scores.detach().cpu().numpy().tolist()
        for i in range(batch_size):
            node_list = nodes_list[i]
            candidates = dict()
            for j in range(len(node_list)):
                node = node_list[j][0]
                path = node_list[j][1]
                if node in candidates and scores[i][j] > candidates[node][0]:
                    candidates[node][0] = scores[i][j]
                else:
                    candidates[node] = [scores[i][j], path]
            candidate = [candidates[c] for c in candidates]
            candidate.sort(key=lambda c: c[0], reverse=True)
            new_nodes = candidate[:self.bs_width]
            outer[i] = [n[1] for n in new_nodes]
            current_graph[i] += [n[-1] for n in outer[i]]
        max_graph_size = max([len(g) for g in current_graph])
        padded_graph = [g + [1 for i in range(max_graph_size - len(g))] for g in current_graph]
        graph_mask = [[1 for i in range(len(g))] + [0 for i in range(max_graph_size - len(g))] for g in current_graph]
        graph_mask = use_cuda(torch.Tensor(graph_mask).long())
        graph_values = self.entity_embedding(use_cuda(torch.Tensor(padded_graph).long()))
        graph_keys = self.graph_attn_linear(graph_values)

        return graph_keys, graph_values, graph_mask

    def init_hidden(self, num_layer, batch_size, hidden_size):
        return (use_cuda(Variable(torch.zeros(num_layer, batch_size, hidden_size))),
                use_cuda(Variable(torch.zeros(num_layer, batch_size, hidden_size))))

    def construct_graph(self, subgraphs, edges):
        graph_list = []
        for i in range(len(subgraphs)):
            g2l = dict()
            graph = dgl.DGLGraph()
            graph.add_nodes(len(subgraphs[i]))
            for index in range(len(subgraphs[i])):
                g2l[subgraphs[i][index]] = index
            edge_heads, edge_tails = [g2l[u] for u in edges[i][0]], [g2l[u] for u in edges[i][1]]
            graph.add_edges(edge_heads, edge_tails)
            node_embed = self.entity_embedding(use_cuda(torch.LongTensor(subgraphs[i])))
            graph.ndata['h'] = node_embed
            graph_list.append(graph)
        return graph_list

    def gnn(self, graph, input):
        gat_output = self.GAT(graph, input)
        graph.ndata['h'] = gat_output
        graph_list = dgl.unbatch(graph)

        gat_output = []
        for i in range(len(graph_list)):
            node_features = graph_list[i].ndata['h'].squeeze(1)
            gat_output.append(node_features)
        return nn_utils.rnn.pad_sequence(gat_output, batch_first=True, padding_value=0)

