package com.core.ui.config;

import com.core.data.ConfigOption;
import com.jfoenix.controls.JFXComboBox;
import javafx.scene.Node;
import javafx.stage.Stage;

public class SelectConfigItem extends BaseConfigItem {
    private JFXComboBox<String> comboBox = new JFXComboBox<>();

    public SelectConfigItem(Stage stage, ConfigOption option) {
        super(stage, option);
        comboBox.setMaxWidth(Double.MAX_VALUE);
        comboBox.getItems().addAll(option.getSelect());
        comboBox.getSelectionModel().select(option.getValue());
        comboBox.getSelectionModel().selectedItemProperty().addListener(((observable, oldValue, newValue) -> {
            if (newValue == null) {
                return;
            }

            getOption().setValue(newValue);
        }));
    }

    @Override
    public Node getNode() {
        return comboBox;
    }
}
