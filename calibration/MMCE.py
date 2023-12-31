import torch
import torch.nn.functional as F
import torch.nn as nn


class MMCE_weighted(nn.Module):
    """
    Computes MMCE_w loss.
    """

    def __init__(self):
        super(MMCE_weighted, self).__init__()
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    def torch_kernel(self, matrix):
        return torch.exp(-1.0 * torch.abs(matrix[:, :, 0] - matrix[:, :, 1]) / (0.4))

    def get_pairs(self, tensor1, tensor2):
        correct_prob_tiled = tensor1.unsqueeze(1).repeat(1, tensor1.shape[0]).unsqueeze(2)
        incorrect_prob_tiled = tensor2.unsqueeze(1).repeat(1, tensor2.shape[0]).unsqueeze(2)

        correct_prob_pairs = torch.cat([correct_prob_tiled, correct_prob_tiled.permute(1, 0, 2)],
                                       dim=2)
        incorrect_prob_pairs = torch.cat([incorrect_prob_tiled, incorrect_prob_tiled.permute(1, 0, 2)],
                                         dim=2)

        correct_prob_tiled_1 = tensor1.unsqueeze(1).repeat(1, tensor2.shape[0]).unsqueeze(2)
        incorrect_prob_tiled_1 = tensor2.unsqueeze(1).repeat(1, tensor1.shape[0]).unsqueeze(2)

        correct_incorrect_pairs = torch.cat([correct_prob_tiled_1, incorrect_prob_tiled_1.permute(1, 0, 2)],
                                            dim=2)
        return correct_prob_pairs, incorrect_prob_pairs, correct_incorrect_pairs

    def get_out_tensor(self, tensor1, tensor2):
        return torch.mean(tensor1 * tensor2)

    def forward(self, input, target):
        input = input.view(-1)
        target = target.view(-1)  # For CIFAR-10 and CIFAR-100, target.shape is [N] to begin with

        predicted_probs = torch.where(input >= 0.5, input, 1-input)
        predicted_labels = torch.where(input >= 0.5, 1, 0)

        correct_mask = torch.where(torch.eq(predicted_labels, target),
                                   torch.ones(predicted_labels.shape).to(self.device),
                                   torch.zeros(predicted_labels.shape).to(self.device))

        k = torch.sum(correct_mask).type(torch.int64)
        k_p = torch.sum(1.0 - correct_mask).type(torch.int64)
        cond_k = torch.where(torch.eq(k, 0), torch.tensor(0).to(self.device), torch.tensor(1).to(self.device))
        cond_k_p = torch.where(torch.eq(k_p, 0), torch.tensor(0).to(self.device), torch.tensor(1).to(self.device))
        k = torch.max(k, torch.tensor(1).to(self.device)) * cond_k * cond_k_p + (1 - cond_k * cond_k_p) * 2
        k_p = torch.max(k_p, torch.tensor(1).to(self.device)) * cond_k_p * cond_k + ((1 - cond_k_p * cond_k) *
                                                                                     (correct_mask.shape[0] - 2))

        correct_prob, _ = torch.topk(predicted_probs * correct_mask, k)
        incorrect_prob, _ = torch.topk(predicted_probs * (1 - correct_mask), k_p)

        correct_prob_pairs, incorrect_prob_pairs, \
        correct_incorrect_pairs = self.get_pairs(correct_prob, incorrect_prob)

        correct_kernel = self.torch_kernel(correct_prob_pairs)
        incorrect_kernel = self.torch_kernel(incorrect_prob_pairs)
        correct_incorrect_kernel = self.torch_kernel(correct_incorrect_pairs)

        sampling_weights_correct = torch.mm((1.0 - correct_prob).unsqueeze(1), (1.0 - correct_prob).unsqueeze(0))

        correct_correct_vals = self.get_out_tensor(correct_kernel,
                                                   sampling_weights_correct)
        sampling_weights_incorrect = torch.mm(incorrect_prob.unsqueeze(1), incorrect_prob.unsqueeze(0))

        incorrect_incorrect_vals = self.get_out_tensor(incorrect_kernel,
                                                       sampling_weights_incorrect)
        sampling_correct_incorrect = torch.mm((1.0 - correct_prob).unsqueeze(1), incorrect_prob.unsqueeze(0))

        correct_incorrect_vals = self.get_out_tensor(correct_incorrect_kernel,
                                                     sampling_correct_incorrect)

        correct_denom = torch.sum(1.0 - correct_prob)
        incorrect_denom = torch.sum(incorrect_prob)

        m = torch.sum(correct_mask)
        n = torch.sum(1.0 - correct_mask)
        mmd_error = 1.0 / (m * m + 1e-5) * torch.sum(correct_correct_vals)
        mmd_error += 1.0 / (n * n + 1e-5) * torch.sum(incorrect_incorrect_vals)
        mmd_error -= 2.0 / (m * n + 1e-5) * torch.sum(correct_incorrect_vals)
        return torch.max(
            (cond_k * cond_k_p).type(torch.FloatTensor).to(self.device).detach() * torch.sqrt(mmd_error + 1e-10),
            torch.tensor(0.0).to(self.device))
