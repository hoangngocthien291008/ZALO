import os
import sys
import json
import argparse
import torch
from torch.utils.data import Dataset

try:
    import transformers.utils.import_utils as _iu
    _iu._torchaudio_available = False
except Exception:
    pass

from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    TrainingArguments,
    Trainer,
    DataCollatorForSeq2Seq
)
from peft import (
    LoraConfig,
    get_peft_model,
    prepare_model_for_kbit_training,
    TaskType
)

class ChatMLJsonDataset(Dataset):
    def __init__(self, jsonl_path, tokenizer, max_length=512):
        self.samples = []
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            raw_lines = [json.loads(line) for line in f if line.strip()]

        for item in raw_lines:
            messages = item['messages']
            # Prompt (system + user)
            prompt_msgs = messages[:-1]
            full_msgs = messages

            prompt_text = tokenizer.apply_chat_template(prompt_msgs, tokenize=False, add_generation_prompt=True)
            full_text = tokenizer.apply_chat_template(full_msgs, tokenize=False, add_generation_prompt=False)

            prompt_ids = tokenizer(prompt_text, truncation=True, max_length=max_length)['input_ids']
            full_enc = tokenizer(full_text, truncation=True, max_length=max_length)
            input_ids = full_enc['input_ids']
            attention_mask = full_enc['attention_mask']

            # Mask prompt tokens with -100 so loss is computed ONLY on the assistant JSON output
            labels = list(input_ids)
            prompt_len = min(len(prompt_ids), len(labels))
            for i in range(prompt_len):
                labels[i] = -100

            self.samples.append({
                'input_ids': input_ids,
                'attention_mask': attention_mask,
                'labels': labels
            })

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]

def main():
    parser = argparse.ArgumentParser(description="QLoRA Fine-Tuning for Zalo AI Challenge DataMate (<=15B SLM)")
    parser.add_argument("--base_model", type=str, default="Qwen/Qwen2.5-14B-Instruct", help="Base model (<=15B params)")
    parser.add_argument("--train_file", type=str, default="sft_train_data.jsonl", help="Path to SFT JSONL dataset")
    parser.add_argument("--output_dir", type=str, default="qlora_adapter", help="Directory to save LoRA adapter")
    parser.add_argument("--epochs", type=float, default=1.5, help="Number of training epochs")
    parser.add_argument("--batch_size", type=int, default=4, help="Per-device batch size")
    parser.add_argument("--grad_accum", type=int, default=4, help="Gradient accumulation steps")
    parser.add_argument("--lr", type=float, default=2e-4, help="Learning rate")
    parser.add_argument("--lora_r", type=int, default=16, help="LoRA rank r")
    parser.add_argument("--lora_alpha", type=int, default=32, help="LoRA alpha")
    args = parser.parse_args()

    # Ensure SFT dataset exists
    if not os.path.exists(args.train_file):
        print(f"[QLoRA] Generating SFT training data at {args.train_file}...")
        from prepare_qlora_data import build_sft_dataset
        base_dir = os.path.dirname(os.path.abspath(__file__))
        train_candidates = [
            os.path.join(base_dir, '..', 'DATA MATE(which we will choose)', 'train'),
            '/content/data/train',
            '/content/DATA MATE(which we will choose)/train',
        ]
        train_dir = next((p for p in train_candidates if os.path.exists(p)), None)
        if train_dir:
            build_sft_dataset(train_dir, args.train_file)
        else:
            raise FileNotFoundError(f"Could not find {args.train_file} or train data directory.")

    print(f"[QLoRA] Loading tokenizer and 4-bit quantized base model: {args.base_model}")
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    compute_dtype = torch.bfloat16 if (torch.cuda.is_available() and torch.cuda.is_bf16_supported()) else torch.float16
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=compute_dtype,
        bnb_4bit_use_double_quant=True
    )

    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        quantization_config=bnb_config,
        device_map="auto",
        trust_remote_code=True
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(model)

    peft_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=0.05,
        bias="none",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]
    )
    model = get_peft_model(model, peft_config)
    model.print_trainable_parameters()

    dataset = ChatMLJsonDataset(args.train_file, tokenizer, max_length=512)
    print(f"[QLoRA] Loaded {len(dataset)} tokenized ChatML samples. Starting QLoRA fine-tuning...")

    training_args = TrainingArguments(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        fp16=(compute_dtype == torch.float16),
        bf16=(compute_dtype == torch.bfloat16),
        logging_steps=10,
        save_strategy="no",
        optim="paged_adamw_8bit",
        lr_scheduler_type="cosine",
        warmup_steps=10,
        report_to="none"
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        data_collator=DataCollatorForSeq2Seq(tokenizer, pad_to_multiple_of=8, return_tensors="pt", padding=True)
    )

    trainer.train()

    os.makedirs(args.output_dir, exist_ok=True)
    model.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    print(f"[QLoRA] Successfully saved fine-tuned LoRA adapter to: {args.output_dir}")

if __name__ == "__main__":
    main()
