plugins {
    id("com.android.application")
}

android {
    namespace = "proekt.luch.app"
    compileSdk = 36

    defaultConfig {
        applicationId = "proekt.luch.app"
        minSdk = 26            // Android 8.0 — ниже нет нормальных каналов уведомлений
        targetSdk = 34
        versionCode = 8
        versionName = "1.7"
    }

    buildFeatures {
        buildConfig = true
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // Отладочная подпись: APK ставится на телефон без всяких ключей.
            // Для публикации ключ надо будет создать отдельно.
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
