# ! pip install git+https://github.com/Lightning-AI/Finetune-miniLM
# ! curl -L https://bit.ly/yelp_train --create-dirs -o ${HOME}/data/yelp/train.csv -C -
# ! curl -L https://bit.ly/yelp_test --create-dirs -o ${HOME}/data/yelp/test.csv -C -
import lightning as L
import torch
import transformers
import finetune_minilm as f_lm


class Finetune(L.LightningWork):
    def __init__(self, *args, tb_drive, **kwargs):
        super().__init__(*args, **kwargs)
        self.tensorboard_drive = tb_drive
        self.lightningignore = ("checkpoints",)

    def run(self):
        f_lm.warn_if_drive_not_empty(self.tensorboard_drive)

        L.seed_everything(777, workers=True)
        tokenizer = self.configure_tokenizer()
        train_dataloader = self.configure_data("~/data/yelp/train.csv", shuffle=True, batch_size=16, tokenizer=tokenizer)
        val_dataloader = self.configure_data("~/data/yelp/test.csv", batch_size=32, tokenizer=tokenizer)
        lightning_module = self.configure_module()
        trainer = L.Trainer(
            max_epochs=5, limit_train_batches=100, limit_val_batches=100,
            strategy="ddp", precision=16, accelerator="auto", devices="auto",
            callbacks=self.configure_callbacks(), log_every_n_steps=5,
            logger=f_lm.DriveTensorBoardLogger(save_dir=".", drive=self.tensorboard_drive),
        )

        trainer.fit(lightning_module, train_dataloader, val_dataloader)

    def configure_module(self) -> L.LightningModule:
        # https://github.com/microsoft/unilm/tree/master/minilm#english-pre-trained-models. 33M parameters
        module = f_lm.TextEmbedder(backbone="microsoft/MiniLM-L12-H384-uncased")
        return EmbeddingSimilarity(module)

    def configure_tokenizer(self):
        return transformers.AutoTokenizer.from_pretrained("microsoft/MiniLM-L12-H384-uncased")

    def configure_data(self, path: str, **kwargs) -> torch.utils.data.DataLoader:
        return f_lm.TokenizedDataloader(dataset=f_lm.TextDataset(csv_file=path), **kwargs)

    def configure_callbacks(self):
        early_stopping = L.pytorch.callbacks.EarlyStopping(monitor="val_loss", min_delta=0.00, verbose=True, mode="min")
        checkpoints = L.pytorch.callbacks.ModelCheckpoint(save_top_k=3, monitor="val_loss", mode="min")
        return [early_stopping, checkpoints]


class EmbeddingSimilarity(L.LightningModule):
    """
    Loosely based on "Sentence-BERT: Sentence Embeddings using Siamese BERT-Networks": https://arxiv.org/abs/1908.10084

    Finetunes a Bert-based model (from HF) for classification by minimizing the cosine similarity loss of text pairs.
    """

    def __init__(self, module: f_lm.TextEmbedder):
        super().__init__()
        self.module = module

    def training_step(self, batch):
        x, y = batch
        embeddings = self.module(x)
        loss = f_lm.pairwise_cosine_embedding_loss(embeddings, y)
        self.log("train_loss_epoch", loss, prog_bar=True, on_step=False, on_epoch=True, sync_dist=True)
        self.log("train_loss_step", loss)
        return loss

    def validation_step(self, batch, _):
        x, y = batch
        embeddings = self.module(x)
        loss = f_lm.pairwise_cosine_embedding_loss(embeddings, y)
        self.log("val_loss", loss, prog_bar=True, sync_dist=True)
        return loss

    def configure_optimizers(self):
        optimizer = torch.optim.AdamW(self.parameters(), lr=5e-5, weight_decay=0.001)
        scheduler = transformers.get_linear_schedule_with_warmup(
            optimizer, num_warmup_steps=5, num_training_steps=self.trainer.estimated_stepping_batches
        )
        return [optimizer], [scheduler]


component = f_lm.TrainerWithTensorboard(Finetune, L.CloudCompute("gpu-fast", disk_size=50))
app = L.LightningApp(component)
